# -*- coding: utf-8 -*-
# Copyright (C) 2012 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
from __future__ import absolute_import, division, print_function, unicode_literals

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, _base, as_completed
from concurrent.futures.thread import _WorkItem
from contextlib import contextmanager
from enum import Enum
from errno import EPIPE, ESHUTDOWN
from functools import wraps
from itertools import cycle
import json
import logging  # lgtm [py/import-and-import-from]
from logging import CRITICAL, Formatter, NOTSET, StreamHandler, WARN, getLogger
import os
from os.path import dirname, isdir, isfile, join
import signal
import sys
from threading import Event, Thread
from time import sleep, time

from .compat import StringIO, iteritems, on_win
from .constants import NULL
from .path import expand
from .._vendor.auxlib.decorators import memoizemethod
from .._vendor.auxlib.logz import NullHandler
from .._vendor.auxlib.type_coercion import boolify
from .._vendor.tqdm import tqdm

from ..common.compat import ensure_binary

log = getLogger(__name__)


class DeltaSecondsFormatter(Formatter):
    """
    Logging formatter with additional attributes for run time logging.

    Attributes:
      `delta_secs`:
        Elapsed seconds since last log/format call (or creation of logger).
      `relative_created_secs`:
        Like `relativeCreated`, time relative to the initialization of the
        `logging` module but conveniently scaled to seconds as a `float` value.
    """
    def __init__(self, fmt=None, datefmt=None):
        self.prev_time = time()
        super(DeltaSecondsFormatter, self).__init__(fmt=fmt, datefmt=datefmt)

    def format(self, record):
        now = time()
        prev_time = self.prev_time
        self.prev_time = max(self.prev_time, now)
        record.delta_secs = now - prev_time
        record.relative_created_secs = record.relativeCreated / 1000
        return super(DeltaSecondsFormatter, self).format(record)


if boolify(os.environ.get('CONDA_TIMED_LOGGING')):
    _FORMATTER = DeltaSecondsFormatter(
        "%(relative_created_secs) 7.2f %(delta_secs) 7.2f "
        "%(levelname)s %(name)s:%(funcName)s(%(lineno)d): %(message)s"
    )
else:
    _FORMATTER = Formatter(
        "%(levelname)s %(name)s:%(funcName)s(%(lineno)d): %(message)s"
    )


def dashlist(iterable, indent=2):
    return ''.join('\n' + ' ' * indent + '- ' + str(x) for x in iterable)


class ContextDecorator(object):
    """Base class for a context manager class (implementing __enter__() and __exit__()) that also
    makes it a decorator.
    """

    # TODO: figure out how to improve this pattern so e.g. swallow_broken_pipe doesn't have to be instantiated  # NOQA

    def __call__(self, f):
        @wraps(f)
        def decorated(*args, **kwds):
            with self:
                return f(*args, **kwds)
        return decorated


class SwallowBrokenPipe(ContextDecorator):
    # Ignore BrokenPipeError and errors related to stdout or stderr being
    # closed by a downstream program.

    def __enter__(self):
        pass

    def __exit__(self, exc_type, exc_val, exc_tb):
        if (exc_val
                and isinstance(exc_val, EnvironmentError)
                and getattr(exc_val, 'errno', None)
                and exc_val.errno in (EPIPE, ESHUTDOWN)):
            return True


swallow_broken_pipe = SwallowBrokenPipe()


class CaptureTarget(Enum):
    """Constants used for contextmanager captured.

    Used similarly like the constants PIPE, STDOUT for stdlib's subprocess.Popen.
    """
    STRING = -1
    STDOUT = -2


@contextmanager
def env_vars(var_map=None, callback=None):

    if var_map is None:
        var_map = {}

    new_var_map = {}
    for name, value in iteritems(var_map):
        new_var_map[str(ensure_binary(name))] = str(ensure_binary(value))
    saved_vars = {}
    for name, value in iteritems(new_var_map):
        saved_vars[name] = os.environ.get(name, NULL)
        os.environ[name] = value
    try:
        if callback:
            callback(True)
        yield
    finally:
        for name, value in iteritems(saved_vars):
            if value is NULL:
                del os.environ[name]
            else:
                os.environ[name] = value
        if callback:
            callback(False)

@contextmanager
def env_var(name, value, callback=None):
# Maybe, but in env_vars, not here:
#    from conda.compat import ensure_fs_path_encoding
#    d = dict({name: ensure_fs_path_encoding(value)})
    d = dict({name: value})
    with env_vars(d, callback=callback) as es:
        yield es

@contextmanager
def env_unmodified(callback=None):
    with env_vars(callback=callback) as es:
        yield es



@contextmanager
def captured(stdout=CaptureTarget.STRING, stderr=CaptureTarget.STRING):
    """Capture outputs of sys.stdout and sys.stderr.

    If stdout is STRING, capture sys.stdout as a string,
    if stdout is None, do not capture sys.stdout, leaving it untouched,
    otherwise redirect sys.stdout to the file-like object given by stdout.

    Behave correspondingly for stderr with the exception that if stderr is STDOUT,
    redirect sys.stderr to stdout target and set stderr attribute of yielded object to None.

    Args:
        stdout: capture target for sys.stdout, one of STRING, None, or file-like object
        stderr: capture target for sys.stderr, one of STRING, STDOUT, None, or file-like object

    Yields:
        CapturedText: has attributes stdout, stderr which are either strings, None or the
            corresponding file-like function argument.
    """
    # NOTE: This function is not thread-safe.  Using within multi-threading may cause spurious
    # behavior of not returning sys.stdout and sys.stderr back to their 'proper' state
    # """
    # Context manager to capture the printed output of the code in the with block
    #
    # Bind the context manager to a variable using `as` and the result will be
    # in the stdout property.
    #
    # >>> from conda.common.io import captured
    # >>> with captured() as c:
    # ...     print('hello world!')
    # ...
    # >>> c.stdout
    # 'hello world!\n'
    # """
    class CapturedText(object):
        pass
    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    if stdout == CaptureTarget.STRING:
        sys.stdout = outfile = StringIO()
    else:
        outfile = stdout
        if outfile is not None:
            sys.stdout = outfile
    if stderr == CaptureTarget.STRING:
        sys.stderr = errfile = StringIO()
    elif stderr == CaptureTarget.STDOUT:
        sys.stderr = errfile = outfile
    else:
        errfile = stderr
        if errfile is not None:
            sys.stderr = errfile
    c = CapturedText()
    log.info("overtaking stderr and stdout")
    try:
        yield c
    finally:
        if stdout == CaptureTarget.STRING:
            c.stdout = outfile.getvalue()
        else:
            c.stdout = outfile
        if stderr == CaptureTarget.STRING:
            c.stderr = errfile.getvalue()
        elif stderr == CaptureTarget.STDOUT:
            c.stderr = None
        else:
            c.stderr = errfile
        sys.stdout, sys.stderr = saved_stdout, saved_stderr
        log.info("stderr and stdout yielding back")


@contextmanager
def argv(args_list):
    saved_args = sys.argv
    sys.argv = args_list
    try:
        yield
    finally:
        sys.argv = saved_args


@contextmanager
def _logger_lock():
    logging._acquireLock()
    try:
        yield
    finally:
        logging._releaseLock()


@contextmanager
def disable_logger(logger_name):
    logr = getLogger(logger_name)
    _lvl, _dsbld, _prpgt = logr.level, logr.disabled, logr.propagate
    null_handler = NullHandler()
    with _logger_lock():
        logr.addHandler(null_handler)
        logr.setLevel(CRITICAL + 1)
        logr.disabled, logr.propagate = True, False
    try:
        yield
    finally:
        with _logger_lock():
            logr.removeHandler(null_handler)  # restore list logr.handlers
            logr.level, logr.disabled = _lvl, _dsbld
            logr.propagate = _prpgt


@contextmanager
def stderr_log_level(level, logger_name=None):
    logr = getLogger(logger_name)
    _hndlrs, _lvl, _dsbld, _prpgt = logr.handlers, logr.level, logr.disabled, logr.propagate
    handler = StreamHandler(sys.stderr)
    handler.name = 'stderr'
    handler.setLevel(level)
    handler.setFormatter(_FORMATTER)
    with _logger_lock():
        logr.setLevel(level)
        logr.handlers, logr.disabled, logr.propagate = [], False, False
        logr.addHandler(handler)
        logr.setLevel(level)
    try:
        yield
    finally:
        with _logger_lock():
            logr.handlers, logr.level, logr.disabled = _hndlrs, _lvl, _dsbld
            logr.propagate = _prpgt


def attach_stderr_handler(level=WARN, logger_name=None, propagate=False, formatter=None):
    # get old stderr logger
    logr = getLogger(logger_name)
    old_stderr_handler = next((handler for handler in logr.handlers if handler.name == 'stderr'),
                              None)

    # create new stderr logger
    new_stderr_handler = StreamHandler(sys.stderr)
    new_stderr_handler.name = 'stderr'
    new_stderr_handler.setLevel(NOTSET)
    new_stderr_handler.setFormatter(formatter or _FORMATTER)

    # do the switch
    with _logger_lock():
        if old_stderr_handler:
            logr.removeHandler(old_stderr_handler)
        logr.addHandler(new_stderr_handler)
        logr.setLevel(level)
        logr.propagate = propagate


def timeout(timeout_secs, func, *args, **kwargs):
    """Enforce a maximum time for a callable to complete.
    Not yet implemented on Windows.
    """
    default_return = kwargs.pop('default_return', None)
    if on_win:
        # Why does Windows have to be so difficult all the time? Kind of gets old.
        # Guess we'll bypass Windows timeouts for now.
        try:
            return func(*args, **kwargs)
        except KeyboardInterrupt:  # pragma: no cover
            return default_return
    else:
        class TimeoutException(Exception):
            pass

        def interrupt(signum, frame):
            raise TimeoutException()

        signal.signal(signal.SIGALRM, interrupt)
        signal.alarm(timeout_secs)

        try:
            ret = func(*args, **kwargs)
            signal.alarm(0)
            return ret
        except (TimeoutException,  KeyboardInterrupt):  # pragma: no cover
            return default_return


class Spinner(object):
    """
    Args:
        message (str):
            A message to prefix the spinner with. The string ': ' is automatically appended.
        enabled (bool):
            If False, usage is a no-op.
        json (bool):
           If True, will not output non-json to stdout.

    """

    # spinner_cycle = cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
    spinner_cycle = cycle('/-\\|')

    def __init__(self, message, enabled=True, json=False):
        self.message = message
        self.enabled = enabled
        self.json = json

        self._stop_running = Event()
        self._spinner_thread = Thread(target=self._start_spinning)
        self._indicator_length = len(next(self.spinner_cycle)) + 1
        self.fh = sys.stdout
        self.show_spin = enabled and not json and hasattr(self.fh, "isatty") and self.fh.isatty()

    def start(self):
        if self.show_spin:
            self._spinner_thread.start()
        elif not self.json:
            self.fh.write("...working... ")
            self.fh.flush()

    def stop(self):
        if self.show_spin:
            self._stop_running.set()
            self._spinner_thread.join()
            self.show_spin = False

    def _start_spinning(self):
        try:
            while not self._stop_running.is_set():
                self.fh.write(next(self.spinner_cycle) + ' ')
                self.fh.flush()
                sleep(0.10)
                self.fh.write('\b' * self._indicator_length)
        except EnvironmentError as e:
            if e.errno in (EPIPE, ESHUTDOWN):
                self.stop()
            else:
                raise

    @swallow_broken_pipe
    def __enter__(self):
        if not self.json:
            sys.stdout.write("%s: " % self.message)
            sys.stdout.flush()
        self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        if not self.json:
            with swallow_broken_pipe:
                if exc_type or exc_val:
                    sys.stdout.write("failed\n")
                else:
                    sys.stdout.write("done\n")
                sys.stdout.flush()


class ProgressBar(object):

    def __init__(self, description, enabled=True, json=False):
        """
        Args:
            description (str):
                The name of the progress bar, shown on left side of output.
            enabled (bool):
                If False, usage is a no-op.
            json (bool):
                If true, outputs json progress to stdout rather than a progress bar.
                Currently, the json format assumes this is only used for "fetch", which
                maintains backward compatibility with conda 4.3 and earlier behavior.
        """
        self.description = description
        self.enabled = enabled
        self.json = json

        if json:
            pass
        elif enabled:
            bar_format = "{desc}{bar} | {percentage:3.0f}% "
            try:
                self.pbar = tqdm(desc=description, bar_format=bar_format, ascii=True, total=1,
                                 file=sys.stdout)
            except EnvironmentError as e:
                if e.errno in (EPIPE, ESHUTDOWN):
                    self.enabled = False
                else:
                    raise

    def update_to(self, fraction):
        try:
            if self.json and self.enabled:
                sys.stdout.write('{"fetch":"%s","finished":false,"maxval":1,"progress":%f}\n\0'
                                 % (self.description, fraction))
            elif self.enabled:
                self.pbar.update(fraction - self.pbar.n)
        except EnvironmentError as e:
            if e.errno in (EPIPE, ESHUTDOWN):
                self.enabled = False
            else:
                raise

    def finish(self):
        self.update_to(1)

    @swallow_broken_pipe
    def close(self):
        if self.enabled and self.json:
            sys.stdout.write('{"fetch":"%s","finished":true,"maxval":1,"progress":1}\n\0'
                             % self.description)
            sys.stdout.flush()
        elif self.enabled:
            self.pbar.close()


class ThreadLimitedThreadPoolExecutor(ThreadPoolExecutor):

    def __init__(self, max_workers=10):
        super(ThreadLimitedThreadPoolExecutor, self).__init__(max_workers)

    def submit(self, fn, *args, **kwargs):
        """
        This is an exact reimplementation of the `submit()` method on the parent class, except
        with an added `try/except` around `self._adjust_thread_count()`.  So long as there is at
        least one living thread, this thread pool will not throw an exception if threads cannot
        be expanded to `max_workers`.

        In the implementation, we use "protected" attributes from concurrent.futures (`_base`
        and `_WorkItem`). Consider vendoring the whole concurrent.futures library
        as an alternative to these protected imports.

        https://github.com/agronholm/pythonfutures/blob/3.2.0/concurrent/futures/thread.py#L121-L131  # NOQA
        https://github.com/python/cpython/blob/v3.6.4/Lib/concurrent/futures/thread.py#L114-L124
        """
        with self._shutdown_lock:
            if self._shutdown:
                raise RuntimeError('cannot schedule new futures after shutdown')

            f = _base.Future()
            w = _WorkItem(f, fn, args, kwargs)

            self._work_queue.put(w)
            try:
                self._adjust_thread_count()
            except RuntimeError:
                # RuntimeError: can't start new thread
                # See https://github.com/conda/conda/issues/6624
                if len(self._threads) > 0:
                    # It's ok to not be able to start new threads if we already have at least
                    # one thread alive.
                    pass
                else:
                    raise
            return f


as_completed = as_completed

def get_instrumentation_record_file():
    default_record_file = join('~', '.conda', 'instrumentation-record.csv')
    return expand(os.environ.get("CONDA_INSTRUMENTATION_RECORD_FILE", default_record_file))


class time_recorder(ContextDecorator):  # pragma: no cover
    record_file = get_instrumentation_record_file()
    start_time = None
    total_call_num = defaultdict(int)
    total_run_time = defaultdict(float)

    def __init__(self, entry_name=None, module_name=None):
        self.entry_name = entry_name
        self.module_name = module_name

    def _set_entry_name(self, f):
        if self.entry_name is None:
            if hasattr(f, '__qualname__'):
                entry_name = f.__qualname__
            else:
                entry_name = ':' + f.__name__
            if self.module_name:
                entry_name = '.'.join((self.module_name, entry_name))
            self.entry_name = entry_name

    def __call__(self, f):
        self._set_entry_name(f)
        return super(time_recorder, self).__call__(f)

    def __enter__(self):
        enabled = os.environ.get('CONDA_INSTRUMENTATION_ENABLED')
        if enabled and boolify(enabled):
            self.start_time = time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.start_time:
            entry_name = self.entry_name
            end_time = time()
            run_time = end_time - self.start_time
            self.total_call_num[entry_name] += 1
            self.total_run_time[entry_name] += run_time
            self._ensure_dir()
            with open(self.record_file, 'a') as fh:
                fh.write("%s,%f\n" % (entry_name, run_time))
            # total_call_num = self.total_call_num[entry_name]
            # total_run_time = self.total_run_time[entry_name]
            # log.debug('%s %9.3f %9.3f %d', entry_name, run_time, total_run_time, total_call_num)

    @classmethod
    def log_totals(cls):
        enabled = os.environ.get('CONDA_INSTRUMENTATION_ENABLED')
        if not (enabled and boolify(enabled)):
            return
        log.info('=== time_recorder total time and calls ===')
        for entry_name in sorted(cls.total_run_time.keys()):
            log.info(
                'TOTAL %9.3f % 9d %s',
                cls.total_run_time[entry_name],
                cls.total_call_num[entry_name],
                entry_name,
            )

    @memoizemethod
    def _ensure_dir(self):
        if not isdir(dirname(self.record_file)):
            os.makedirs(dirname(self.record_file))


def print_instrumentation_data():  # pragma: no cover
    record_file = get_instrumentation_record_file()

    grouped_data = defaultdict(list)
    final_data = {}

    if not isfile(record_file):
        return

    with open(record_file) as fh:
        for line in fh:
            entry_name, total_time = line.strip().split(',')
            grouped_data[entry_name].append(float(total_time))

    for entry_name in sorted(grouped_data):
        all_times = grouped_data[entry_name]
        counts = len(all_times)
        total_time = sum(all_times)
        average_time = total_time / counts
        final_data[entry_name] = {
            'counts': counts,
            'total_time': total_time,
            'average_time': average_time,
        }

    print(json.dumps(final_data, sort_keys=True, indent=2, separators=(',', ': ')))


if __name__ == "__main__":
    print_instrumentation_data()
