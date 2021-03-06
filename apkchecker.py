# -*- coding: utf-8 -*-
import calendar
import hashlib
import json
from multiprocessing import Process, Manager
import os
import shlex
import subprocess
from subprocess import PIPE
import sys
from datetime import datetime
import time
import collections
import traceback
from com.dtmilano.android.viewclient import ViewClient
import re


class LogcatWatcher(object):
    def __init__(self, serialno, log_verbose, package_name):
        self.serialno = serialno
        self.log_verbose = log_verbose
        self.package = package_name
        self.logcat_proc = self.start_logcat()

    def start_logcat(self):
        # clear log before starting logcat
        adb_clear_cmd = shlex.split('adb -s {0} logcat -c'.format(self.serialno))
        adb_clear = subprocess.Popen(adb_clear_cmd)
        while adb_clear.poll() is None:
            pass
        adb_logcat_cmd = shlex.split('adb -s {0} logcat'.format(self.serialno))
        return subprocess.Popen(adb_logcat_cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE)

    def watch_logcat(self, logcat_data, logcat_result):
        # locat regex filter
        LOG_LINE = re.compile(r'^([A-Z])/(.+?)\( *(\d+)\): (.*?)$')
        BUG_LINE = re.compile(r'.*nativeGetEnabledTags.*')
        BACKTRACE_LINE = re.compile(r'^#(.*?)pc\s(.*?)$')

        pids = set()
        app_pid = None
        last_log_time = time.time()
        # keep reading form logcat subprocess
        while self.logcat_proc.poll() is 0 or self.logcat_proc.poll() is None:
            elapsed_time = time.time() - last_log_time
            if elapsed_time > 10:
                break
            line = self.logcat_proc.stdout.readline().decode('utf-8', 'replace').strip()
            if len(line) == 0:
                break
            bug_line = BUG_LINE.match(line)
            if bug_line is not None:
                continue
            log_line = LOG_LINE.match(line)
            if log_line is None:
                continue
            force_write_log = False
            level, tag, owner, message = log_line.groups()
            # parse start proc log to get package pid
            start = self._parse_start_proc(line)
            if start is not None:
                line_package, target, line_pid, line_uid, line_gids = start
                if self.package in line_package:
                    app_pid = line_pid
                    pids.add(app_pid)
                    force_write_log = True
            # parse death proc log
            dead_pid, dead_pname = self._parse_death_proc(tag, message, self.package, pids)
            if dead_pid:
                pids.remove(dead_pid)
                logcat_result['is_passed'] = 0
                force_write_log = True
            # make sure the backtrace is printed after a native crash
            if tag.strip() == 'DEBUG':
                bt_line = BACKTRACE_LINE.match(message.lstrip())
                if bt_line is not None:
                    message = message.lstrip()
                    owner = app_pid
            # filter logcat by pid
            if not force_write_log:
                if owner not in pids:
                    continue
            # now we have desired logcat content
            last_log_time = time.time()
            log_content = {
                'timestamp': calendar.timegm(datetime.now().utctimetuple()),
                'type': 'logcat',
                'tag': tag,
                'text': message,
                'level': level
            }
            if self.log_verbose:
                print >> sys.stdout, log_content
            logcat_data.append(log_content)
        self.logcat_proc.kill()
        logcat_result['is_passed'] = 1

    @staticmethod
    def _parse_start_proc(line):
        PID_START = re.compile(r'^.*: Start proc ([a-zA-Z0-9._:]+) for ([a-z]+ [^:]+): pid=(\d+) uid=(\d+) gids=(.*)$')
        PID_START_DALVIK = re.compile(
            r'^E/dalvikvm\(\s*(\d+)\): >>>>> ([a-zA-Z0-9._:]+) \[ userId:0 \| appId:(\d+) \]$')
        start = PID_START.match(line)
        if start is not None:
            line_package, target, line_pid, line_uid, line_gids = start.groups()
            return line_package, target, line_pid, line_uid, line_gids
        start = PID_START_DALVIK.match(line)
        if start is not None:
            line_pid, line_package, line_uid = start.groups()
            return line_package, '', line_pid, line_uid, ''
        return None

    @staticmethod
    def _parse_death_proc(tag, message, package, pids):
        PID_KILL = re.compile(r'^Killing (\d+):([a-zA-Z0-9._:]+)/[^:]+: (.*)$')
        PID_LEAVE = re.compile(r'^No longer want ([a-zA-Z0-9._:]+) \(pid (\d+)\): .*$')
        PID_DEATH = re.compile(r'^Process ([a-zA-Z0-9._:]+) \(pid (\d+)\) has died.?$')
        if tag != 'ActivityManager':
            return None, None
        kill = PID_KILL.match(message)
        if kill:
            pid = kill.group(1)
            package_line = kill.group(2)
            if package in package_line and pid in pids:
                return pid, package_line
        leave = PID_LEAVE.match(message)
        if leave:
            pid = leave.group(2)
            package_line = leave.group(1)
            if package in package_line and pid in pids:
                return pid, package_line
        death = PID_DEATH.match(message)
        if death:
            pid = death.group(2)
            package_line = death.group(1)
            if package in package_line and pid in pids:
                return pid, package_line
        return None, None


def logcat_runner(serialno, log_verbose, package, logcat_data, logcat_result):
    logcat_watcher = LogcatWatcher(serialno, log_verbose, package)
    logcat_watcher.watch_logcat(logcat_data, logcat_result)


class ApkChecker(object):
    def __init__(self, conf_file):
        # initialize check_result dict
        self.result = collections.defaultdict(lambda: collections.defaultdict(dict))
        self.result['running_log'] = []
        self._check_not_finished()
        self._check_not_passed()
        self.conf_data = self.read_conf(conf_file)
        # manipulate locat data between process
        manager = Manager()
        self.logcat_data = manager.list()
        self.logcat_result = manager.dict()

        try:
            self.apk_file = self.conf_data['apk_file']
            self.serialno = self.conf_data['serialno']
            self.screenshot_path = self.conf_data['screenshot_path']
            self.log_verbose = self.conf_data['log_verbose']
        except KeyError:
            self._error_log(traceback.format_exc())

        # get apk file basic info
        self.get_apk_info()
        self.package = self.result['apk_result']['package_name']
        self.activity = self.result['apk_result']['launch_activity']
        # begin connect to phone
        self.adb = self.connect()

    def read_conf(self, conf_file):
        if not os.path.exists(conf_file):
            self._error_log('apk conf_file: {0} not found'.format(conf_file))
        return json.load(open(conf_file))

    def get_apk_info(self):
        if not os.path.exists(self.apk_file):
            self._error_log('apk file: {0} not found'.format(self.apk_file))
        # get apk file info
        self.result['apk_result']['file_name'] = os.path.basename(self.apk_file)
        self.result['apk_result']['file_size'] = os.path.getsize(self.apk_file)
        self.result['apk_result']['file_md5'] = hashlib.md5(open(self.apk_file, 'rb').read()).hexdigest()
        # dump apk package info
        ret = self._run_wrapper('aapt dump badging {0}'.format(self.apk_file))
        # get apk package info
        self.result['apk_result']['package_name'] = re.search("(?<=package: name=')[^']+", ret).group()
        self.result['apk_result']['version_code'] = re.search("(?<=versionCode=')[^']+", ret).group()
        self.result['apk_result']['version_name'] = re.search("(?<=versionName=')[^']+", ret).group()
        self.result['apk_result']['launch_activity'] = re.search("(?<=launchable-activity: name=')[^']+", ret).group()

    def connect(self):
        # check devices is connected
        ret = self._run_wrapper('adb devices')
        if self.serialno not in ret:
            self._error_log('device {0} not connected'.format(self.serialno))
        # connecting to device
        try:
            _adb, _serialno = ViewClient.connectToDeviceOrExit(verbose=False, serialno=self.serialno)
            return _adb
        except Exception as e:
            self._error_log('connecting to Android Device {0} Failed: {1}'.format(self.serialno, e))

    def run_check(self):
        self.unlock_device()
        self.install_apk()
        self.gather_info()
        logcat_watcher = self.start_logcat_daemon()
        self.start_app()
        while logcat_watcher.is_alive():
            self.gather_info()
        self._save_logcat_data()
        self.stop_app()
        self._check_finished()
        self.lock_device()
        self._save_result()

    def unlock_device(self):
        try:
            # wake screen on
            self.adb.wake()
            # try unlock screen
            if self.adb.isLocked():
                (w, h) = (self.adb.getProperty("display.width"), self.adb.getProperty("display.height"))
                self.adb.drag((w * 0.5, h * 0.7), (w, h * 0.7))
        except Exception as e:
            self._error_log('device not support screen unlock: {0}'.format(e))

    def lock_device(self):
        if self.adb.isScreenOn():
            self.adb.shell('input keyevent POWER')

    def install_apk(self):
        ret = self._run_wrapper('adb -s {0} install -r {1}'.format(self.serialno, self.apk_file))
        if "Failure " in ret:
            self._error_log('install {0} to device {1} failed: {2}'.format(self.apk_file, self.serialno, ret))

    def start_app(self):
        self._run_wrapper('adb -s {0} shell am start -n {1}/{2}'.format(self.serialno, self.package, self.activity))

    def stop_app(self):
        self._run_wrapper('adb -s {0} shell am force-stop {1}'.format(self.serialno, self.package))

    def is_app_alive(self):
        return True if self.package in self.adb.shell('ps') else False

    def gather_info(self):
        timestamp = self.get_timestamp()
        self._data_log(timestamp=timestamp, cpu_data=self.get_cpu_data(), mem_data=self.get_mem_data(),
                       screenshot=self.take_screenshot(timestamp))

    def get_mem_data(self):
        ret = self.adb.shell('dumpsys meminfo {0}'.format(self.package))
        mem_regex = re.search("(?<=TOTAL)\s+\d+", ret)
        mem = mem_regex.group().lstrip() if mem_regex else 0
        return round(float(mem) / 1024, 2)

    def get_cpu_data(self):
        ret = self.adb.shell('dumpsys cpuinfo')
        cpu_line = filter(lambda line: self.package in line, ret.splitlines())
        if cpu_line:
            cpu_regex = re.search("\d+(?=% )", cpu_line[0])
            cpu = cpu_regex.group()
        else:
            cpu = 0
        return round(float(cpu), 2)

    def take_screenshot(self, timestamp):
        full_file_path = os.path.join(self.screenshot_path, str(timestamp) + '.png')
        self.adb.takeSnapshot(reconnect=True).save(full_file_path, 'PNG')
        return '{0}.png'.format(timestamp)

    def start_logcat_daemon(self):
        logcat_watcher_proc = Process(target=logcat_runner, args=(
            self.serialno, self.log_verbose, self.package, self.logcat_data, self.logcat_result))
        logcat_watcher_proc.daemon = True
        logcat_watcher_proc.start()
        return logcat_watcher_proc

    def _run_wrapper(self, cmd):
        ret = self.run_cmd(cmd)
        if ret['retcode'] != 0:
            self._error_log('command: {0} execution failed, return: {0}'.format(cmd, ret['retval']))
        self._cmd_log(cmd, ret['retval'])
        return ret['retval']

    @staticmethod
    def run_cmd(cmd, cwd=None, daemon=False):
        args = map(lambda s: s.decode('utf-8'), shlex.split(cmd.encode('utf-8')))
        child = subprocess.Popen(args, stdout=PIPE, stderr=PIPE, cwd=cwd)
        if not daemon:
            stdout, stderr = child.communicate()
            retcode = child.returncode
            if retcode is 0:
                result = {'retcode': retcode, 'retval': stdout}
            else:
                result = {'retcode': retcode, 'retval': stderr}
            return result
        else:
            return child.pid

    @staticmethod
    def get_timestamp():
        return calendar.timegm(datetime.now().utctimetuple())

    def _error_log(self, content):
        log_content = {
            'timestamp': self.get_timestamp(),
            'content': content
        }
        self._check_not_finished()
        self.result['error_log'] = log_content
        self._save_result()
        print >> sys.stderr, log_content
        sys.exit(1)

    def _cmd_log(self, cmd, ret, level='v'):
        log_content = {
            'timestamp': self.get_timestamp(),
            'type': 'cmd',
            'level': level,
            'cmd': cmd,
            'ret': ret
        }
        self.result['running_log'].append(log_content)
        if self.log_verbose:
            print >> sys.stdout, log_content

    def _save_logcat_data(self):
        self.result['apk_result']['passed'] = self.logcat_result['is_passed']
        self.result['running_log'].extend(self.logcat_data)

    def _data_log(self, timestamp, cpu_data, mem_data, screenshot):
        log_content = {
            'timestamp': timestamp,
            'type': 'data',
            'cpu_data': cpu_data,
            'mem_data': mem_data,
            'screenshot': screenshot
        }
        self.result['running_log'].append(log_content)
        if self.log_verbose:
            print >> sys.stdout, log_content

    def _check_finished(self):
        self.result['apk_result']['finished'] = 1

    def _check_not_finished(self):
        self.result['apk_result']['finished'] = 0

    def _check_passed(self):
        self.result['apk_result']['passed'] = 1

    def _check_not_passed(self):
        self.result['apk_result']['passed'] = 0

    def _save_result(self, filename='check_result.json'):
        with open(filename, 'w') as outfile:
            json.dump(self.result, outfile, sort_keys=True, indent=4, separators=(',', ': '))


if __name__ == '__main__':
    apk_checker = ApkChecker('test_conf.json')
    apk_checker.run_check()
