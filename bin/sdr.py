import fcntl
import json
import logging
import os
import pickle
import pwd
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from enum import Enum
from multiprocessing import Process
from telnetlib import Telnet
from typing import Optional, List, Union

import smpplib.client
import smpplib.consts
import smpplib.gsm
import audioread


class AtomicOpen:
    """
    Atomic read/write file
    """
    # Open the file with arguments provided by user. Then acquire
    # a lock on that file object (WARNING: Advisory locking).
    def __init__(self, path, *args, **kwargs):
        # Open the file and acquire a lock on the file before operating
        self.file = open(path, *args, **kwargs)
        # Lock the opened file
        if self.file.writable():
            fcntl.lockf(self.file, fcntl.LOCK_EX)

    # Return the opened file object (knowing a lock has been obtained).
    def __enter__(self, *args, **kwargs):
        return self.file

    # Unlock the file and close the file object.
    def __exit__(self, exc_type=None, exc_value=None, tb=None):
        # Flush to make sure all buffered contents are written to file.
        self.file.flush()
        os.fsync(self.file.fileno())
        # Release the lock on the file.
        if self.file.writable():
            fcntl.lockf(self.file, fcntl.LOCK_UN)
        self.file.close()
        # Handle exceptions that may have come up during execution, by
        # default any exceptions are raised to the user.
        return exc_type is None


class Subscriber:
    """
    Subscriber info
    """

    def __init__(self, imsi, msisdn, imei, last_seen, cell, calls_status, sms_status, failed_pagings):
        self.imsi = imsi
        self.msisdn = msisdn
        self.imei = imei
        self.last_seen = last_seen
        self.failed_pagings = failed_pagings
        self.cell = cell
        self.short_cell = "/".join(cell.split("/")[-2:])
        self.calls_status = calls_status
        self.sms_status = sms_status

    def __repr__(self):
        return f"imsi={self.imsi}, msisdn={self.msisdn}, imei={self.imei}, cell={self.cell}, " \
               f"calls={self.calls_status}, sms={self.sms_status}"

    def __str__(self):
        return self.__repr__()

    @property
    def last_seen_int(self):
        return int(self.last_seen) if self.last_seen.isnumeric() else 0


########################################################################################################################
#         For process call logs                                                                                        #
########################################################################################################################
class CallStatus(Enum):
    """
    Call statuses
    """
    NEW = "Будет совершен звонок"
    NOT_AVAILABLE = "Абонент недоступен"
    AVAILABLE = "Абонент доступен"
    INIT = "Инициализация звонка"
    RINGING = "Сигнал"
    ACTIVE = "Идет звонок"
    REJECT_BY_USER = "Звонок отклонен"
    UP = "Абонент ответил"
    HANGUP = "Звонок прекращен"
    HANGUP_BY_USER = "Звонок прекращен абонентом"
    HANGUP_BY_BTS = "Звонок прекращен БТС"
    BREAK_BY_BTS = "Инициализация прервана БТС"
    STOP_BY_BTS = "Звонок остановлен БТС во время сигнала"
    UNKNOWN = "Неизвестный переход состояний"

    def is_final(self):
        """
        Whether the status is final for the call
        """
        return self in [self.NOT_AVAILABLE, self.REJECT_BY_USER, self.HANGUP_BY_USER, self.HANGUP_BY_BTS,
                        self.BREAK_BY_BTS, self.STOP_BY_BTS]


class CallState(Enum):
    """
    Call states
    """
    NULL = "NULL"
    CALL_PRESENT = "CALL_PRESENT"
    MO_TERM_CALL_CONF = "MO_TERM_CALL_CONF"
    CALL_RECEIVED = "CALL_RECEIVED"
    CONNECT_REQUEST = "CONNECT_REQUEST"
    ACTIVE = "ACTIVE"
    DISCONNECT_IND = "DISCONNECT_IND"
    RELEASE_REQ = "RELEASE_REQ"
    BROKEN_BY_BTS = "BROKEN_BY_BTS"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    NEW = "NEW"


class CallStateEvent:
    """
    Change call state event
    """

    def __init__(self, state: CallState, prev_state: CallState, event_time: str):
        self._state = state
        self._prev_state = prev_state
        self._event_time: str = event_time

    def status(self):
        return self._state

    def status_time(self):
        return self._event_time

    def prev_status(self):
        return self._prev_state

    def __repr__(self):
        return f"{self._event_time}: {self._prev_state.name} -> {self._state.name}"


class Call:
    """
    Call info
    """
    # available state transitions
    _statuses = {
        (CallState.NEW, CallState.NULL): CallStatus.NEW,
        (CallState.NULL, CallState.CALL_PRESENT): CallStatus.AVAILABLE,
        (CallState.NULL, CallState.BROKEN_BY_BTS): CallStatus.BREAK_BY_BTS,
        (CallState.NULL, CallState.NOT_AVAILABLE): CallStatus.NOT_AVAILABLE,
        (CallState.CALL_PRESENT, CallState.RELEASE_REQ): CallStatus.BREAK_BY_BTS,
        (CallState.CALL_PRESENT, CallState.NULL): CallStatus.BREAK_BY_BTS,
        (CallState.CALL_PRESENT, CallState.MO_TERM_CALL_CONF): CallStatus.INIT,
        (CallState.RELEASE_REQ, CallState.NULL): None,
        (CallState.MO_TERM_CALL_CONF, CallState.RELEASE_REQ): CallStatus.BREAK_BY_BTS,
        (CallState.MO_TERM_CALL_CONF, CallState.NULL): CallStatus.BREAK_BY_BTS,
        (CallState.MO_TERM_CALL_CONF, CallState.CALL_RECEIVED): CallStatus.RINGING,
        (CallState.CALL_RECEIVED, CallState.DISCONNECT_IND): CallStatus.REJECT_BY_USER,
        (CallState.DISCONNECT_IND, CallState.RELEASE_REQ): CallStatus.HANGUP_BY_USER,
        (CallState.CALL_RECEIVED, CallState.CONNECT_REQUEST): CallStatus.UP,
        (CallState.CONNECT_REQUEST, CallState.ACTIVE): CallStatus.ACTIVE,
        (CallState.ACTIVE, CallState.DISCONNECT_IND): CallStatus.HANGUP,
        (CallState.DISCONNECT_IND, CallState.NULL): CallStatus.HANGUP_BY_BTS,
        (CallState.CALL_RECEIVED, CallState.RELEASE_REQ): CallStatus.STOP_BY_BTS,
        (CallState.CALL_RECEIVED, CallState.NULL): CallStatus.STOP_BY_BTS,

    }

    __LOG_NAME = os.path.dirname(os.path.abspath(__file__)) + "/calls_error.log"

    def __init__(self, imsi: str, callref: str, tid: str):
        self.imsi = imsi
        self.callref = callref
        self.tid = tid
        self.events = []
        self.statuses = []
        self.status: Optional[CallStatus] = None

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        return f"{self.status.value}/{self.get_last_state()}({self.get_last_event_time()})"

    def _save_error(self, error: str):
        with open(self.__LOG_NAME, "a+") as f:
            f.write(error)
            f.write("\n")

    def add_event(self, event: CallStateEvent, tid):
        self.events.append(event)
        self.tid = tid
        if not self.status or not self.status.is_final():
            if (event.prev_status(), event.status()) in self._statuses:
                new_status = self._statuses[(event.prev_status(), event.status())]
            else:
                self._save_error(f"Unknown event: {event.prev_status().name} -> {event.status().name}")
                new_status = CallStatus.UNKNOWN
            self.status = new_status or self.status
            self.statuses.append(self.status)

    def is_over(self):
        """
        Is the call over
        """
        return self.get_last_state() in [CallState.NOT_AVAILABLE, CallState.BROKEN_BY_BTS] or \
               (self.get_last_state() == CallState.NULL and len(self.events) > 1)

    def get_last_state(self):
        return self.events[-1].status()

    def get_last_event_time(self):
        return self.events[-1].status_time()

    def get_info(self):
        return {
            "ended": self.is_over(),
            "imsi": self.imsi,
            "last_time": self.get_last_event_time(),
            "status": self.status.value
        }


class EventLine:
    """
    BTS event
    """
    # regexp for search
    _templates = [
        re.compile("trans\(CC.*IMSI-([0-9]+):")  # IMSI
        , re.compile("callref-0x([0-9a-f]+) ")  # callref
        , re.compile(" tid-([0-9]+)[,)]")  # tid
        , re.compile("^(.*) bts")  # time
        , re.compile(" new state (.+) -> .+$")  # prev state
        , re.compile(" new state .+ -> (.+)$")  # new state
        , re.compile("tid-255.* (Paging expired)$")  # expired
        , re.compile(" (New transaction)$")  # new_transaction
        , re.compile("^.* bts.*(Started Osmocom Mobile Switching Center)")  # service started
        , re.compile("tid-255.* tx (MNCC_REL_CNF)$")
    ]

    _exclude = re.compile("callref-0x([48])[0-9a-f]{7}")

    def __init__(self):
        self.imsi = ""
        self.callref = ""
        self.tid = ""
        self.event_time = ""
        self.event: Optional[CallStateEvent] = None
        self.is_started_event = False

    def __repr__(self):
        prefix = f"{self.imsi}/{self.callref}/{self.tid}"
        if self.is_started_event:
            return f"{self.event_time}: Osmocom started"
        else:
            return f"{prefix}: {self.event.prev_status().name} -> {self.event.status().name}"

    @classmethod
    def create(cls, line):
        if cls._exclude.search(line):
            return None

        results = []
        for template in cls._templates:
            match = template.search(line)
            results.append(match.group(1) if match else "")

        event = EventLine()

        imsi, callref, tid, event_time, from_state, to_state, expired, new_transaction, started, bts_break = results
        event.imsi = imsi
        event.callref = callref
        event.tid = tid
        event.event_time = event_time
        if new_transaction:
            event.event = CallStateEvent(CallState.NULL, CallState.NEW, event_time)
        elif started:
            event.is_started_event = True
        elif bts_break:
            event.event = CallStateEvent(CallState.BROKEN_BY_BTS, CallState.NULL, event_time)
        elif expired:
            event.event = CallStateEvent(CallState.NOT_AVAILABLE, CallState.NULL, event_time)
        elif from_state and to_state:
            event.event = CallStateEvent(CallState(to_state), CallState(from_state), event_time)
        else:
            raise Exception("Unknown event")

        return event


class CallTimestamp:
    """
    Call event log info
    """
    __SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    __FILE_NAME = "call_timestamp"
    __WORK_STATUS = "work"
    __STOP_STATUS = "stop"

    def __init__(self):
        state_file = f"{self.__SCRIPT_DIR}/{self.__FILE_NAME}"
        if os.path.isfile(state_file):
            while True:
                try:
                    with AtomicOpen(state_file, "rb") as f:
                        obj = pickle.load(f)
                        for key, value in obj.__dict__.items():
                            self.__dict__[key] = value
                    break
                except Exception:
                    pass
        else:
            self._status = self.__STOP_STATUS
            self._call_start = None
            self._call_stop = None
            self._last_call_log_since = None
            self._last_call_log_until = None
            self._all_logs = {}
            self._logs = {}
            self._start_time = None
            self._dump()

    def _str_from_dt(self, dt):
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _dump(self):
        dump_file = f"{self.__SCRIPT_DIR}/{self.__FILE_NAME}"
        with AtomicOpen(dump_file, "wb") as f:
            pickle.dump(self, f)

    def start_calls(self):
        if self._status == self.__WORK_STATUS:
            return
        self._status = self.__WORK_STATUS
        dt = datetime.now().replace(microsecond=0)
        self._call_start = dt
        self._call_stop = None
        self._last_call_log_since = dt
        self._last_call_log_until = dt

        self._dump()

    def stop_calls(self):
        self._status = self.__STOP_STATUS
        self._call_stop = datetime.now().replace(microsecond=0)
        self._dump()

    def get_log(self):
        if self._last_call_log_until is None:
            return []

        since = self._last_call_log_until
        until = datetime.now().replace(microsecond=0) if self._status == self.__WORK_STATUS \
            else self._call_stop + timedelta(seconds=30)
        until = until if datetime.now().replace(microsecond=0) > until else datetime.now().replace(microsecond=0)
        lines = []
        if since < until:
            self._last_call_log_since = self._last_call_log_until
            self._last_call_log_until = until
            res = subprocess.run(
                ["bash", "-c", f"journalctl -q -u osmo-msc --since='{self._str_from_dt(since)}' "
                               f"--until='{self._str_from_dt(until)}'"],
                capture_output=True)
            lines = res.stdout.decode("UTF-8").split("\n")
        records = self._process_logs(lines)
        self._dump()
        return records

    def _process_logs(self, lines: List[str]):
        # pre filter
        lines = [line.strip() for line in lines if ("Started Osmocom" in line or
                                                    (" New transaction" in line and "trans(CC" in line) or
                                                    " new state " in line or
                                                    (" Paging expired" in line and "trans(CC" in line) or
                                                    ("tid-255,PAGING) tx MNCC_REL_CNF" in line and "trans(CC" in line))
                 and "tid-8" not in line
                 ]

        all_logs = self._all_logs.copy()
        logs = self._logs.copy()
        start_time = self._start_time

        for line in lines:
            event = EventLine.create(line)
            if event is None:
                continue
            if event.is_started_event:
                if logs:
                    all_logs[f"{start_time}-{event.event_time}"] = logs
                    logs = {}
                start_time = event.event_time
            elif event.event.prev_status() == CallState.NEW:
                start_time = start_time or event.event_time
                new_call = Call(imsi=event.imsi, callref=event.callref, tid=event.tid)
                new_call.add_event(event.event, event.tid)
                if event.imsi not in logs:
                    logs[event.imsi] = {}
                if event.callref in logs[event.imsi]:
                    print("\n\n", event.callref)
                    raise Exception("Same callref")

                logs[event.imsi][event.callref] = new_call

            elif event.callref == "0":
                if event.imsi in logs:
                    event_calls = logs[event.imsi]
                    for event_call in event_calls.values():
                        if event_call.tid == event.tid and event_call.get_last_state() not in [CallState.NULL,
                                                                                               CallState.BROKEN_BY_BTS,
                                                                                               CallState.NOT_AVAILABLE]:
                            event_call.add_event(event.event, event.tid)
                            break

            else:
                if event.imsi in logs and event.callref in logs[event.imsi]:
                    event_call = logs[event.imsi][event.callref]
                    event_call.add_event(event.event, event.tid)

        self._all_logs = all_logs.copy()
        self._logs = logs.copy()
        self._start_time = start_time

        if logs:
            all_logs[f"{start_time}-"] = logs

        return all_logs


class SmsTimestamp:
    """
    Sms event log info
    """
    __FILE_NAME = os.path.dirname(os.path.abspath(__file__)) + "/sms_timestamp"
    __sms_period_time = 20

    def __init__(self):
        if os.path.isfile(self.__FILE_NAME):
            while True:
                try:
                    with AtomicOpen(self.__FILE_NAME, "rb") as f:
                        obj = pickle.load(f)
                        for key, value in obj.__dict__.items():
                            self.__dict__[key] = value
                    break
                except Exception:
                    pass
        else:
            self._start = None
            self._stop = None
            self._dump()

    def _dump(self):
        with AtomicOpen(self.__FILE_NAME, "wb") as f:
            pickle.dump(self, f)

    def start(self):
        dt = datetime.now().replace(microsecond=0)
        self._start = dt
        self._stop = None

        self._dump()

    def stop(self):
        self._stop = datetime.now().replace(microsecond=0)
        self._dump()

    def get_period(self):
        until = self._stop or datetime.now().replace(microsecond=0)
        since = until

        if self._start is not None:
            since = until - timedelta(seconds=self.__sms_period_time)

            if since < self._start:
                since = self._start

        since = since.strftime("%Y-%m-%d %H:%M:%S")
        until = until.strftime("%Y-%m-%d %H:%M:%S")
        return since, until


########################################################################################################################

class CallType(Enum):
    MP3 = 1,
    GSM = 2,
    SILENT = 3


class OfflineTacFilter:
    """
    Filter subscribers by TAC
    """
    _base = None
    _base_path = os.path.dirname(os.path.abspath(__file__)) + "/tac_filtered.json"

    @classmethod
    def __init_base(cls):
        cls._base = {}
        if not os.path.exists(cls._base_path):
            print(f"OfflineTacFilter: {cls._base_path} not found")
            return
        with open(cls._base_path) as f:
            try:
                cls._base = json.load(f)
            except Exception as e:
                print(f"OfflineTacFilter: {repr(e)}")

    def __init__(self):
        if self._base is None:
            self.__init_base()

    def is_filtered(self, imei):
        return imei[:8] in self._base


class Sdr:
    """
    Main class
    """

    TOTAL_TCHF = "TCH/F total"
    TOTAL_TCHH = "TCH/H total"
    TOTAL_SDCCH8 = "SDCCH8 total"
    USED_TCHF = "TCH/F used"
    USED_TCHH = "TCH/H used"
    USED_SDCCH8 = "SDCCH8 used"
    _tac_filter = OfflineTacFilter()

    def __init__(self, msc_host: str = "localhost", msc_port_vty: int = 4254,
                 smpp_host: str = "localhost", smpp_port: int = 2775, smpp_id: str = "OSMO-SMPP",
                 smpp_password: str = "1234", debug_output: bool = True, bsc_host: str = "localhost",
                 bsc_port_vty: int = 4242):
        self._msc_host = msc_host
        self._msc_port_vty = msc_port_vty
        self._smpp_host = smpp_host
        self._smpp_port = smpp_port
        self._smpp_id = smpp_id
        self._smpp_password = smpp_password
        self._logger = logging.getLogger("SDR")
        self._bsc_host = bsc_host
        self._bsc_port_vty = bsc_port_vty

        if debug_output and len(self._logger.handlers) == 0:
            self._logger.setLevel(logging.DEBUG)
            handler = logging.StreamHandler(sys.stdout)
            handler.setLevel(logging.DEBUG)
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            self._logger.addHandler(handler)

    def _check_msisdn(self, msisdn):
        """
        Subscriber availability check
        """
        start_cmd = f"subscriber msisdn {msisdn} silent-call start any signalling\r\n".encode()
        stop_cmd = f"subscriber msisdn {msisdn} silent-call stop\r\n".encode()
        expired_cmd = f"enable\r\nsubscriber msisdn {msisdn} expire\r\n".encode()
        with Telnet(self._msc_host, self._msc_port_vty) as tn:
            tn.write(start_cmd)
            try:
                result = tn.expect([b"Silent call success", b"Silent call failed", b"Silent call ended",
                                    b"No subscriber found for", b"Subscriber not attached",
                                    b"Cannot start silent call"], 11)

                if result[0] == 0:  # success
                    tn.write(stop_cmd)
                    tn.expect([b"% Silent call stopped"], 2)
                    return "ok"
                elif result[0] in (-1, 1):  # timeout
                    tn.write(expired_cmd)
                    return "expired"

            except EOFError as e:
                print(f"SDRError: {traceback.format_exc()}")
            return "error"

    def _silent_call(self, msisdn, result_list, channel="tch/h", silent_call_type="speech-amr"):
        """
        Make a silent call
        """
        start_cmd = f"subscriber msisdn {msisdn} silent-call start {channel} {silent_call_type}\r\n".encode()
        stop_cmd = f"subscriber msisdn {msisdn} silent-call stop\r\n".encode()
        with Telnet(self._msc_host, self._msc_port_vty) as tn:
            tn.write(start_cmd)
            try:
                result = tn.expect([b"Silent call success", b"Silent call failed", b"Silent call ended",
                                    b"No subscriber found for", b"Subscriber not attached",
                                    b"Cannot start silent call"], 11)

                if result[0] == 0:  # success
                    time.sleep(3)
                    tn.write(stop_cmd)
                    tn.expect([b"% Silent call stopped"], 2)
                    result_list.append(("ok", msisdn))
                    return "ok", msisdn
                elif result[0] in (-1, 1):  # timeout
                    tn.write(stop_cmd)
                    result_list.append(("expired", msisdn))
                    return "expired", msisdn

            except EOFError as e:
                print(f"SDRError: {traceback.format_exc()}")

            result_list.append(("error", msisdn))
            tn.write(stop_cmd)
            return "error", msisdn

    def _get_subscribers(self):
        """
        Get subscriber list from Osmocom
        """
        start_cmd = f"subscriber list\r\n".encode()
        subscribers = []
        with Telnet(self._msc_host, self._msc_port_vty) as tn:
            tn.write(start_cmd)
            try:
                result = tn.expect([b"subscriber list end"], 11)

                analyze = False
                if result[0] == 0:  # success
                    for line in result[2].split(b"\r\n"):
                        if line == b"subscriber list begin":
                            analyze = True
                        elif line == b"subscriber list end":
                            break
                        elif analyze:
                            elements = line.decode("ascii").split(",")
                            subscribers.append(
                                Subscriber(elements[0], elements[1], elements[2], elements[3], elements[4], [], [],
                                           elements[5]))

            except EOFError as e:
                print(f"SDRError: {traceback.format_exc()}")

            # filter by TAC
            subscribers = [subscriber for subscriber in subscribers
                           if not self._tac_filter.is_filtered(subscriber.imei)]
            return subscribers

    def _clear_expired(self):
        """
        Delete unavailable subscribers
        """
        threads = [threading.Thread(target=self._check_msisdn, args=(subscriber.msisdn,))
                   for subscriber in self._get_subscribers()]
        list(map(lambda x: x.start(), threads))

        for index, thread in enumerate(threads):
            thread.join()

    def silent_call(self, channel="tch/h", silent_call_type="speech-amr"):
        """
        Silent call to all
        """
        subscribers = self._get_subscribers()

        attempts = 3
        ok_count = 0
        all_count = len(subscribers)

        while attempts and subscribers:
            attempts -= 1
            results = []
            threads = [threading.Thread(target=self._silent_call,
                                        args=(subscriber.msisdn, results, channel, silent_call_type)) for subscriber
                       in subscribers]
            list(map(lambda x: x.start(), threads))

            for index, thread in enumerate(threads):
                thread.join()

            ok_count += len([1 for result in results if result[0] == "ok"])
            self._logger.debug(f"Silent call ok count {ok_count}/{all_count}")
            repeat_msisdn = [result[1] for result in results if result[0] != "ok"]
            subscribers = [subscriber for subscriber in subscribers if subscriber.msisdn in repeat_msisdn]

        self._logger.debug(f"ok:{ok_count}, fail:{len(subscribers)}")
        return ok_count

    def get_subscribers(self, check_before: bool = False, with_status: bool = False):
        """
        Get subscriber info list
        """
        if check_before:
            self._clear_expired()
        subscribers = self._get_subscribers()

        if with_status:
            call_records = self.calls_status()
            sms_records = self.sms_statuses()

            for subscriber in subscribers:
                subscriber.calls_status = call_records[subscriber.imsi] if subscriber.imsi in call_records else []
                subscriber.sms_status = sms_records[subscriber.imsi] if subscriber.imsi in sms_records else []

        return subscribers

    def call(self, call_type: CallType, call_to: Union[str, List[str]], call_from: str = "00000",
             voice_file: Optional[str] = None, set_call_timestamp: bool = False):
        """
        Call to subscriber|subscriber list
        """

        if set_call_timestamp:
            CallTimestamp().start_calls()

        self._logger.debug(f"{call_type}, {call_to}, {call_from}, {voice_file}")
        asterisk_sounds_path = "/usr/share/asterisk/sounds/en_US_f_Allison/"

        if call_type in (CallType.GSM, CallType.MP3) and voice_file is None:
            raise Exception("Need voice file")

        if call_type == CallType.GSM:
            if os.path.isfile(voice_file):
                os.system(f"cp -f {voice_file} {asterisk_sounds_path}")
                voice_file = os.path.split(voice_file)[1].split(".")[0]
            else:
                if not os.path.isfile(f"{asterisk_sounds_path}{voice_file}.gsm"):
                    raise Exception(f"Not found file: {voice_file}")

        if call_type == CallType.MP3 and not os.path.isfile(voice_file):
            raise Exception(f"Not found file: {voice_file}")

        extension = "gsm" if call_type == CallType.GSM else (
            "mp3" if call_type == CallType.MP3 else "silent")
        data = "" if call_type == CallType.SILENT else f"\nSetvar: voice_file={voice_file}"
        if call_type == CallType.MP3:
            # try get loop count
            loop_count = 5
            try:
                with audioread.audio_open(voice_file) as vf:
                    loop_count = (60 // vf.duration) + 1
            except Exception:
                print(f"Get loop error: {traceback.format_exc()}")

            data += f"\nSetvar: loop_count={int(loop_count)}"

        call_to = call_to if isinstance(call_to, list) else [call_to]

        # write call file for asterisk
        def write_as_asterisk(msisdns):
            r = pwd.getpwnam("asterisk")
            os.setgid(r.pw_gid)
            os.setuid(r.pw_uid)

            umask = os.umask(0)

            idx = 0
            for callee in msisdns:
                call_data = f"Channel: SIP/GSM/{callee}\n" \
                            f"MaxRetries: 500\n" \
                            f"RetryTime: 1\n" \
                            f"WaitTime: 100\n" \
                            f"CallerID: {call_from}\n" \
                            f"Context: calls\n" \
                            f"Extension: {extension}\n" \
                            f"Priority: 1\n" \
                            + data
                idx += 1
                call_file = "/var/spool/asterisk/outgoing/{:06d}.call".format(idx)

                with open(call_file, "w") as f:
                    f.write(call_data)
            os.umask(umask)

        p = Process(target=write_as_asterisk, args=(call_to,))
        p.start()
        p.join()

    def _get_filtered_subscribers(self, exclude_list=None, include_list=None, exclude_2sim=True):
        """
        Filter subscriber list
        (exclude, include, 2sim)
        """
        all_subscibers = sorted(self.get_subscribers(), key=lambda x: x.last_seen_int)
        if include_list is not None:
            include_list = [subscriber[:14] for subscriber in include_list]
            all_subscibers = [subscriber for subscriber in all_subscibers if subscriber.imei in include_list]
        if exclude_list is not None:
            exclude_list = [subscriber[:14] for subscriber in exclude_list]
            all_subscibers = [subscriber for subscriber in all_subscibers if subscriber.imei not in exclude_list]

        exclude_2sim_list = []
        if exclude_2sim:
            for idx, subscriber_1 in enumerate(all_subscibers):
                for subscriber_2 in all_subscibers[idx + 1:]:
                    diff_cnt = sum([1 if subscriber_1.imei[ch_idx] != subscriber_2.imei[ch_idx] else 0 for ch_idx
                                    in range(len(subscriber_1.imei))])
                    if diff_cnt <= 2:
                        exclude_2sim_list.append(subscriber_1 if subscriber_1.last_seen_int > subscriber_2.last_seen_int
                                                 else subscriber_2)
                        break

        return [subscriber for subscriber in all_subscibers if subscriber not in exclude_2sim_list]

    def call_to_all(self, call_type: CallType = CallType.GSM, voice_file: str = "gubin", call_from: str = "00000",
                    exclude_list=None, include_list=None):
        """
        Call to all
        """
        self._logger.debug("Start call_to_all")
        self.set_ho(0)
        self.switch_config(use_sms=False)
        voice_file = None if call_type == CallType.SILENT else voice_file

        all_subscribers = self._get_filtered_subscribers(exclude_list=exclude_list, include_list=include_list)

        bts_list = self.get_bts()
        all_subscribers = [subscriber for subscriber in all_subscribers if subscriber.short_cell in bts_list]

        channels = self.get_channels()
        for bts in bts_list:
            print(f"BTS {bts}:\n")
            for name, value in channels[bts].items():
                print(f"{name} {value}\n")

        CallTimestamp().start_calls()
        self.call(call_type, [subscriber.msisdn for subscriber in all_subscribers], call_from, voice_file)

    def call_to_list(self, call_type: CallType, call_to: Union[str, List[str]], call_from: str = "00000",
                     voice_file: Optional[str] = None):
        """
        Call to list
        """

        self._logger.debug("Start call_to_list")
        self.set_ho(0)
        self.switch_config(use_sms=False)
        voice_file = None if call_type == CallType.SILENT else voice_file

        CallTimestamp().start_calls()
        self.call(call_type, call_to, call_from, voice_file)

    def send_message(self, sms_from: str, sms_to: str, sms_message: str, is_silent: bool):
        """
        Send SMS to subscriber
        """
        client = smpplib.client.Client(self._smpp_host, self._smpp_port)
        client.logger.setLevel(logging.DEBUG)

        # Print when obtain message_id
        client.set_message_sent_handler(
            lambda pdu: sys.stdout.write('sent {} {}\n'.format(pdu.sequence, pdu.message_id)))
        client.set_message_received_handler(
            lambda pdu: sys.stdout.write('delivered {}\n'.format(pdu.receipted_message_id)))

        client.connect()
        client.bind_transceiver(system_id=self._smpp_id, password=self._smpp_password)

        parts, encoding_flag, msg_type_flag = smpplib.gsm.make_parts(sms_message)

        try:
            sms_message.encode("ascii")
            coding = encoding_flag
        except:
            coding = smpplib.consts.SMPP_ENCODING_ISO10646

        self._logger.debug('Sending SMS "%s" to %s' % (sms_message, sms_to))
        for part in parts:
            pdu = client.send_message(
                msg_type=smpplib.consts.SMPP_MSGTYPE_USERACK,
                source_addr_ton=smpplib.consts.SMPP_TON_ALNUM,
                source_addr_npi=smpplib.consts.SMPP_NPI_ISDN,
                source_addr=sms_from if len(sms_from) != 7 else sms_from + " ",
                dest_addr_ton=smpplib.consts.SMPP_TON_INTL,
                dest_addr_npi=smpplib.consts.SMPP_NPI_ISDN,
                destination_addr=sms_to,
                short_message=part,
                data_coding=coding,
                esm_class=msg_type_flag,
                registered_delivery=True,
                protocol_id=64 if is_silent else 0,
            )

        client.state = smpplib.consts.SMPP_CLIENT_STATE_OPEN
        client.disconnect()

    def send_message_to_all(self, sms_from: str, sms_text: str, exclude_list: list = None, include_list: list = None,
                            is_silent: bool = False, once: bool = False):
        """
        Send SMS to all
        """

        self._logger.debug("Start send_message_to_all")
        self.set_ho(0)
        self.switch_config(use_sms=True)
        self.delete_delivered_sms(once)
        subscribers = self._get_filtered_subscribers(include_list=include_list, exclude_list=exclude_list,
                                                     exclude_2sim=False)

        SmsTimestamp().start()

        for subscriber in subscribers:
            self.send_message(sms_from, subscriber.msisdn, sms_text, is_silent)

    def send_message_to_list(self, sms_from: str, sms_text: str, sms_to: Union[str, List[str]],
                             is_silent: bool = False, once: bool = False):
        """
        Send SMS to list
        """

        self._logger.debug("Start send_message_to_list")
        self.set_ho(0)
        self.switch_config(use_sms=True)
        self.delete_delivered_sms(once)

        sms_to = sms_to if isinstance(sms_to, list) else [sms_to]

        SmsTimestamp().start()

        for msisdn in sms_to:
            self.send_message(sms_from, msisdn, sms_text, is_silent)

    def stop_calls(self):
        """
        Stop calls
        """
        self._logger.debug("Stop calls")
        subprocess.run(["bash", "-c", "rm -f /var/spool/asterisk/outgoing/*"])
        subprocess.run(["bash", "-c", 'asterisk -rx "hangup request all"'])
        subprocess.run(["bash", "-c", "rm -f /var/spool/asterisk/outgoing/*"])
        time.sleep(1)
        subprocess.run(["bash", "-c", 'asterisk -rx "hangup request all"'])
        subprocess.run(["bash", "-c", "rm -f /var/spool/asterisk/outgoing/*"])
        CallTimestamp().stop_calls()

    def clear_hlr(self):
        """
        Backup and clear HLR base
        """
        current_path = os.path.dirname(os.path.abspath(__file__))
        subprocess.run(f"bash -c {current_path}/max_stop".split())
        archive_path = f"{current_path}/../tmp/hlr_archive"
        subprocess.run(f"mkdir -p {archive_path}".split())
        archive_file_name = f"hlr_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        db_path = "/var/lib/osmocom"
        subprocess.run(f"mv {db_path}/hlr.db {archive_path}/{archive_file_name}".split())
        subprocess.run(f"bash -c {current_path}/max_start".split())

    def to_850(self):
        """
        BTS2 (bait) -> BTS3 (true)
        """
        current_path = os.path.dirname(os.path.abspath(__file__))
        subprocess.run(f"bash -c {current_path}/850".split())

    def to_900(self):
        """
        BTS3 (true) -> BTS2 (bait)
        """
        current_path = os.path.dirname(os.path.abspath(__file__))
        subprocess.run(f"bash -c {current_path}/900".split())

    def start(self):
        """
        Start BS
        """
        current_path = os.path.dirname(os.path.abspath(__file__))
        subprocess.run(f"bash -c {current_path}/max_start".split())

    def stop(self):
        """
        Stop BS
        """
        self.stop_calls()
        current_path = os.path.dirname(os.path.abspath(__file__))
        subprocess.run(f"bash -c {current_path}/max_stop".split())

    def calls_status(self):
        """
        Return call statuses
        """
        result_records = {}
        records = CallTimestamp().get_log()

        if len(records) > 0:
            last_record = records[list(records.keys())[-1]]

            status_filter = {CallStatus.NOT_AVAILABLE, CallStatus.RINGING, CallStatus.ACTIVE, CallStatus.REJECT_BY_USER,
                             CallStatus.HANGUP}
            for imsi, calls in last_record.items():
                all_statuses = []
                for imsi_call in calls.values():
                    all_statuses.extend(imsi_call.statuses)
                result_records[imsi] = list(status_filter.intersection(all_statuses))
        return result_records

    def calls_status_show(self):
        """
        Return call statuses for show command
        """
        result_records = {}
        records = CallTimestamp().get_log()

        if len(records) > 0:
            last_record = records[list(records.keys())[-1]]

            for imsi, calls in last_record.items():
                result_records[imsi] = [imsi_call.status for imsi_call in calls.values()]
        return result_records

    def sms_statuses(self):
        """
        Return SMS statuses
        """
        since, until = SmsTimestamp().get_period()

        res = subprocess.run(
            ["bash", "-c", f"journalctl -u osmo-msc --since='{since}' --until='{until}' | grep 'stat:DELIVRD'"],
            capture_output=True)
        lines = res.stdout.decode("UTF-8").split("\n")
        records = {}

        template = re.compile("IMSI-([0-9]+)")
        for line in lines:
            search_result = template.search(line)
            if search_result:
                imsi = search_result.group(1)
                if imsi not in records:
                    records[imsi] = []
                records[imsi].append("DELIVERED")
        return records

    def get_bts(self):
        """
        Return BTS info
        """
        ret = []
        cmd = f"show bts\r\n".encode()
        with Telnet(self._bsc_host, self._bsc_port_vty) as tn:
            tn.write(cmd)
            try:
                result = tn.expect([b"ACCH Repetition                  \r\nOsmoBSC", b"not available\)\r\nOsmoBSC"], 2)

                if result[0] != -1:  # success
                    result = [line.decode("utf-8").strip() for line in result[2].split(b"\r\n")]
                    bts = ""
                    re_bts = re.compile("is of sysmobts type in band.*has CI ([0-9]+) LAC ([0-9]+),")
                    for line in result:
                        match = re_bts.search(line)
                        if match:
                            bts = f"{match.group(2)}/{match.group(1)}"
                        if "OML Link state: connected" in line:
                            ret.append(bts)

            except EOFError as e:
                print(f"SDRError: {traceback.format_exc()}")

            return ret

    def get_channels(self):
        """
        Return BTS channels info
        """
        ret = {}
        cmd = f"show bts\r\n".encode()
        with Telnet(self._bsc_host, self._bsc_port_vty) as tn:
            tn.write(cmd)
            try:
                result = tn.expect([b"ACCH Repetition                  \r\nOsmoBSC", b"not available\)\r\nOsmoBSC"], 2)

                if result[0] != -1:  # success
                    result = [line.decode("utf-8").strip() for line in result[2].split(b"\r\n")]
                    bts = ""
                    re_bts = re.compile("is of sysmobts type in band.*has CI ([0-9]+) LAC ([0-9]+),")
                    for line in result:
                        match = re_bts.search(line)
                        if match:
                            bts = f"{match.group(2)}/{match.group(1)}"
                            ret[bts] = {self.TOTAL_TCHF: 0,
                                        self.USED_TCHF: 0,
                                        self.TOTAL_TCHH: 0,
                                        self.USED_TCHH: 0,
                                        self.TOTAL_SDCCH8: 0,
                                        self.USED_SDCCH8: 0}
                        if "Number of TCH/F channels total:" in line:
                            channels_count = int(line.replace("Number of TCH/F channels total:", "").strip())
                            ret[bts][self.TOTAL_TCHF] = channels_count
                        if "Number of TCH/F channels used:" in line:
                            channels_count = int(line.replace("Number of TCH/F channels used:", "").strip())
                            ret[bts][self.USED_TCHF] = channels_count
                        if "Number of TCH/H channels total:" in line:
                            channels_count = int(line.replace("Number of TCH/H channels total:", "").strip())
                            ret[bts][self.TOTAL_TCHH] = channels_count
                        if "Number of TCH/H channels used:" in line:
                            channels_count = int(line.replace("Number of TCH/H channels used:", "").strip())
                            ret[bts][self.USED_TCHH] = channels_count
                        if "Number of SDCCH8 channels total:" in line:
                            channels_count = int(line.replace("Number of SDCCH8 channels total:", "").strip())
                            ret[bts][self.TOTAL_SDCCH8] = channels_count
                        if "Number of SDCCH8 channels used:" in line:
                            channels_count = int(line.replace("Number of SDCCH8 channels used:", "").strip())
                            ret[bts][self.USED_SDCCH8] = channels_count

            except EOFError as e:
                print(f"SDRError: {traceback.format_exc()}")

            return ret

    def set_ho(self, cnt=0):
        """
        Set the required number of handover
        cnt = 0 for stop
        """
        cmd = f"ho_count {cnt}\r\n".encode()
        with Telnet(self._bsc_host, self._bsc_port_vty) as tn:
            tn.write(cmd)

    def handover(self):
        """
        Try do handover
        """
        bts_list = self.get_bts()
        if len(bts_list) != 2:
            return

        all_subscibers = self.get_subscribers()
        all_subscibers = [subscriber for subscriber in all_subscibers if subscriber.short_cell in bts_list]
        counter = {}
        for bts in bts_list:
            counter[bts] = len([1 for subscriber in all_subscibers
                                if subscriber.short_cell == bts and subscriber.last_seen_int < 20])

        bts_0, bts_1 = counter.items()
        bts_name_0, users_0 = bts_0
        bts_name_1, users_1 = bts_1
        total_users = users_0 + users_1

        channels = self.get_channels()
        total_channels_0 = channels[bts_name_0][self.TOTAL_TCHF] + channels[bts_name_0][self.TOTAL_TCHH]
        total_channels_1 = channels[bts_name_1][self.TOTAL_TCHF] + channels[bts_name_1][self.TOTAL_TCHH]

        if total_channels_0 == 0 or total_channels_1 == 0:
            return

        need_ho = int(max(users_0, users_1) - total_users / 2)
        self.set_ho(need_ho)

        call_bts = bts_name_0 if users_0 > users_1 else bts_name_1
        call_subscribers = [subscriber for subscriber in all_subscibers if subscriber.short_cell == call_bts]

        paging_bts = bts_name_1 if call_bts == bts_name_0 else bts_name_0
        paging_subscribers = [subscriber for subscriber in all_subscibers if subscriber.short_cell == paging_bts]

        results = []
        threads = []

        if need_ho > 0:
            threads.extend([threading.Thread(target=self._silent_call,
                                             args=(subscriber.msisdn, results)) for subscriber in call_subscribers])
        else:
            paging_subscribers.extend(call_subscribers)

        threads.extend([threading.Thread(target=self.paging_one,
                                         args=(subscriber.msisdn,)) for subscriber in paging_subscribers])

        list(map(lambda x: x.start(), threads))

        for thread in threads:
            thread.join()

        ok_count = len([1 for result in results if result[0] == "ok"])
        self._logger.debug(f"Silent call with speech ok count {ok_count}/{len(results)}")

    def pprinttable(self, rows):
        """
        Print info as table
        """
        if len(rows) > 0:
            headers = rows[0]
            lens = []
            for i in range(len(rows[0])):
                lens.append(len(max([x[i] for x in rows] + [headers[i]], key=lambda x: len(str(x)))))
            formats = []
            hformats = []
            for i in range(len(rows[0])):
                if isinstance(rows[0][i], int):
                    formats.append("%%%dd" % lens[i])
                else:
                    formats.append("%%-%ds" % lens[i])
                hformats.append("%%-%ds" % lens[i])
            pattern = " | ".join(formats)
            hpattern = " | ".join(hformats)
            separator = "-+-".join(['-' * n for n in lens])
            print(hpattern % tuple(headers))
            print(separator)

            for line in rows[1:]:
                print(pattern % tuple(line))

    def paging_one(self, msisdn):
        """
        Do paging request for subscriber
        """
        with Telnet(self._msc_host, self._msc_port_vty) as tn:
            tn.write(f"subscriber msisdn {msisdn} paging\r\n".encode())
            tn.expect([b"paging subscriber", b"No subscriber found for"], 5)

    def stop_sms(self):
        """
        Stop SMS
        """
        self._logger.debug("Stop sms")
        with Telnet(self._msc_host, self._msc_port_vty) as tn:
            tn.write(b"sms delete all\r\n")
        SmsTimestamp().stop()

    def switch_config(self, use_sms=False):
        """
        Switch configs for SMS|calls
        """
        cmd = b"switch config 1\r\n" if use_sms else b"switch config 0\r\n"
        with Telnet(self._bsc_host, self._bsc_port_vty) as tn:
            tn.write(cmd)

    def delete_delivered_sms(self, delete=False):
        """
        Set delete delivered SMS or not
        """
        cmd = b"sms delete delivered 1\r\n" if delete else b"sms delete delivered 0\r\n"
        with Telnet(self._msc_host, self._msc_port_vty) as tn:
            tn.write(cmd)
