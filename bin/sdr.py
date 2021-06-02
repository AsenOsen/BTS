import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from argparse import ArgumentParser
from datetime import datetime
from enum import Enum
from telnetlib import Telnet
from typing import Optional, List
import pprint

import smpplib.client
import smpplib.consts
import smpplib.gsm
from osmopy.osmo_ipa import Ctrl


class Subscriber:

    def __init__(self, imsi, msisdn, imei, last_seen, cell, calls_status, sms_status):
        self.imsi = imsi
        self.msisdn = msisdn
        self.imei = imei
        self.last_seen = last_seen
        self.cell = cell
        self.calls_status = calls_status
        self.sms_status = sms_status

    def __repr__(self):
        return f"imsi={self.imsi}, msisdn={self.msisdn}, imei={self.imei}, cell={self.cell}, calls={self.calls_status}, sms={self.sms_status}"

    def __str__(self):
        return self.__repr__()


########################################################################################################################
#         For process call logs                                                                                        #
########################################################################################################################
class CallStatus(Enum):
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

    def is_ended(self):
        return self in [self.NOT_AVAILABLE, self.REJECT_BY_USER, self.HANGUP_BY_USER, self.HANGUP_BY_BTS,
                        self.BREAK_BY_BTS, self.STOP_BY_BTS]


class CallState(Enum):
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
        if not self.status or not self.status.is_ended():
            if (event.prev_status(), event.status()) in self._statuses:
                new_status = self._statuses[(event.prev_status(), event.status())]
            else:
                self._save_error(f"Unknown event: {event.prev_status().name} -> {event.status().name}")
                new_status = CallStatus.UNKNOWN
            self.status = new_status or self.status
            self.statuses.append(self.status)

    def is_ended(self):
        return self.get_last_state() in [CallState.NOT_AVAILABLE, CallState.BROKEN_BY_BTS] or \
               (self.get_last_state() == CallState.NULL and len(self.events) > 1)

    def get_last_state(self):
        return self.events[-1].status()

    def get_last_event_time(self):
        return self.events[-1].status_time()

    def get_info(self):
        return {
            "ended": self.is_ended(),
            "imsi": self.imsi,
            "last_time": self.get_last_event_time(),
            "status": self.status.value
        }


class EventLine:
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

    _exclude = re.compile("callref-0x(4|8)[0-9a-f]{7,7}")

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
    __FILE_NAME = os.path.dirname(os.path.abspath(__file__)) + "/call_timestamp"
    __WORK_STATUS = "work"
    __STOP_STATUS = "stop"

    @classmethod
    def start_calls(cls):
        try:
            with open(cls.__FILE_NAME, "r") as f:
                lines = f.readlines()
                if len(lines) == 2 and lines[0].strip() == cls.__WORK_STATUS:
                    return

        except IOError:
            pass

        with open(cls.__FILE_NAME, "w") as f:
            f.writelines([cls.__WORK_STATUS, "\n", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])

    @classmethod
    def stop_calls(cls):
        since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(cls.__FILE_NAME, "r") as f:
                lines = f.readlines()
                if len(lines) == 2:
                    since = lines[1].strip()

        except IOError:
            pass

        with open(cls.__FILE_NAME, "w") as f:
            f.writelines([cls.__STOP_STATUS, "\n", since])

    @classmethod
    def get_since_data(cls):
        try:
            with open(cls.__FILE_NAME, "r") as f:
                lines = f.readlines()
                if len(lines) == 2:
                    return lines[1].strip()

        except IOError:
            pass

        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class SmsTimestamp:
    __FILE_NAME = os.path.dirname(os.path.abspath(__file__)) + "/sms_timestamp"

    @classmethod
    def update(cls):
        with open(cls.__FILE_NAME, "w") as f:
            f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    @classmethod
    def get_since_data(cls):
        try:
            with open(cls.__FILE_NAME, "r") as f:
                lines = f.readlines()
                if len(lines) == 1:
                    return lines[0].strip()
        except IOError:
            pass

        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


########################################################################################################################

class CallType(Enum):
    MP3 = 1,
    GSM = 2,
    SILENT = 3


class Sdr:

    def __init__(self, msc_host: str = "localhost", msc_port_ctrl: int = 4255, msc_port_vty: int = 4254,
                 smpp_host: str = "localhost", smpp_port: int = 2775, smpp_id: str = "OSMO-SMPP",
                 smpp_password: str = "1234", debug_output: bool = False):
        self._msc_host = msc_host
        self._msc_port_ctrl = msc_port_ctrl
        self._msc_port_vty = msc_port_vty
        self._smpp_host = smpp_host
        self._smpp_port = smpp_port
        self._smpp_id = smpp_id
        self._smpp_password = smpp_password
        self._logger = logging.getLogger("SDR")

        if debug_output:
            self._logger.setLevel(logging.DEBUG)
            handler = logging.StreamHandler(sys.stdout)
            handler.setLevel(logging.DEBUG)
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            self._logger.addHandler(handler)

    def _leftovers(self, sck, fl):
        """
        Read outstanding data if any according to flags
        """
        try:
            data = sck.recv(1024, fl)
        except socket.error as _:
            return False
        if len(data) != 0:
            tail = data
            while True:
                (head, tail) = Ctrl().split_combined(tail)
                self._logger.debug("Got message:", Ctrl().rem_header(head))
                if len(tail) == 0:
                    break
            return True
        return False

    def _do_set_get(self, sck, var, value=None):
        (r, c) = Ctrl().cmd(var, value)
        sck.send(c)
        ret = sck.recv(4096)
        return (Ctrl().rem_header(ret),) + Ctrl().verify(ret, r, var, value)

    def _check_msisdn(self, msisdn):
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
                pass
            return "error"

    def _silent_call_speech(self, msisdn, result_list):

        start_cmd = f"subscriber msisdn {msisdn} silent-call start tch/f speech-fr\r\n".encode()
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
                    result_list.append("ok")
                    return "ok"
                elif result[0] in (-1, 1):  # timeout
                    tn.write(stop_cmd)
                    result_list.append("expired")
                    return "expired"

            except EOFError as e:
                pass
            result_list.append("error")
            tn.write(stop_cmd)
            return "error"

    def _get_subscribers(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setblocking(True)
            s.connect((self._msc_host, self._msc_port_ctrl))
            self._leftovers(s, socket.MSG_DONTWAIT)
            (a, _, _) = self._do_set_get(s, "subscriber-list-active-v1")

            def subscriber_from_string(subscriber_string):
                elements = subscriber_string.split(",")
                return Subscriber(elements[0], elements[1], elements[2], elements[3], elements[4], [], [])

            subscribers = a.decode("ascii").split()[3:]
            return [subscriber_from_string(line) for line in subscribers]

    def _clear_expired(self):
        subscribers = self._get_subscribers()
        self._logger.debug(subscribers)
        chunk_size = 10
        chunks = [subscribers[i:i + chunk_size] for i in range(0, len(subscribers), chunk_size)]
        for chunk in chunks:
            threads = [threading.Thread(target=self._check_msisdn, args=(subscriber.msisdn,)) for subscriber in chunk]
            list(map(lambda x: x.start(), threads))

            for index, thread in enumerate(threads):
                self._logger.debug("Main    : before joining thread %d.", index)
                thread.join()
                self._logger.debug("Main    : thread %d done", index)

    def silent_call(self):
        subscribers = self._get_subscribers()

        results = []
        threads = [threading.Thread(target=self._silent_call_speech, args=(subscriber.msisdn, results,)) for subscriber
                   in subscribers]
        list(map(lambda x: x.start(), threads))

        for index, thread in enumerate(threads):
            self._logger.debug("Main    : before joining thread %d.", index)
            thread.join()
            self._logger.debug("Main    : thread %d done", index)

        ok_count = len([1 for result in results if result == "ok"])
        self._logger.debug(f"Silent call with speech ok count {ok_count}/{len(results)}")
        return ok_count

    def get_subscribers(self, check_before: bool = False, with_status: bool = False):
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

    def call(self, call_type: CallType, call_to: str, call_from: str = "00000", voice_file: Optional[str] = None):

        CallTimestamp.start_calls()

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

        application = "Playback" if call_type == CallType.GSM else (
            "MP3Player" if call_type == CallType.MP3 else "Hangup")
        data = "" if call_type == CallType.SILENT else f"\nData: {voice_file}"

        call_data = f"Channel: SIP/GSM/{call_to}\n" \
                    f"MaxRetries: 500\n" \
                    f"RetryTime: 1\n" \
                    f"WaitTime: 30\n" \
                    f"CallerID: {call_from}\n" \
                    f"Application: {application}\n" \
                    + data

        call_file = f"{call_to}.call"
        with open(call_file, "w") as f:
            f.write(call_data)
            f.close()

        os.system(f"chown asterisk:asterisk {call_file}")
        os.system(f"mv {call_file} /var/spool/asterisk/outgoing/")

    def call_to_all(self, call_type: CallType = CallType.GSM, voice_file: str = "gubin", call_from: str = "00000",
                    exclude=False, include=False, call_first_count=None):
        voice_file = None if call_type == CallType.SILENT else voice_file
        exclude_list = []
        current_path = os.path.dirname(os.path.abspath(__file__))
        if exclude:
            with open(current_path + "/exclude_list") as f:
                exclude_list = [line.strip()[:14] for line in f.readlines()]
        elif include:
            with open(current_path + "/include_list") as f:
                include_list = [line.strip()[:14] for line in f.readlines()]

        # update last_seen
        self.silent_call()
        all_subscibers = sorted(self.get_subscribers(),
                                key=lambda x: int(x.last_seen) if x.last_seen.isnumeric() else 0)
        all_subscibers = [subscriber for subscriber in all_subscibers if
                          (exclude and subscriber.imei not in exclude_list) or \
                          (include and subscriber.imei in include_list) or \
                          (not include and not exclude)]

        call_first_count = call_first_count or (len(all_subscibers) // 2)

        # first order calls
        for subscriber in all_subscibers[:call_first_count]:
            if (exclude and subscriber.imei not in exclude_list) or \
                    (include and subscriber.imei in include_list) or \
                    (not include and not exclude):
                self.call(call_type, subscriber.msisdn, call_from, voice_file)

        time.sleep(2)

        # last calls
        for subscriber in all_subscibers[call_first_count:]:
            if (exclude and subscriber.imei not in exclude_list) or \
                    (include and subscriber.imei in include_list) or \
                    (not include and not exclude):
                self.call(call_type, subscriber.msisdn, call_from, voice_file)

    def send_message(self, sms_from: str, sms_to: str, sms_message: str, is_silent: bool):
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
                source_addr=sms_from,
                dest_addr_ton=smpplib.consts.SMPP_TON_INTL,
                dest_addr_npi=smpplib.consts.SMPP_NPI_ISDN,
                destination_addr=sms_to,
                short_message=part,
                data_coding=coding,
                esm_class=msg_type_flag,
                registered_delivery=True,
                protocol_id=64 if is_silent else 0,
            )
            self._logger.debug(pdu.sequence)

        client.state = smpplib.consts.SMPP_CLIENT_STATE_OPEN
        client.disconnect()

    def send_message_to_all(self, sms_from: str, sms_text: str, exclude: bool = False, include: bool = False,
                            is_silent: bool = False):
        subscribers = self.get_subscribers()
        exclude_list = []
        include_list = []
        current_path = os.path.dirname(os.path.abspath(__file__))

        if exclude:
            with open(current_path + "/exclude_list") as f:
                exclude_list = [line.strip()[:14] for line in f.readlines()]
        elif include:
            with open(current_path + "/include_list") as f:
                include_list = [line.strip()[:14] for line in f.readlines()]

        SmsTimestamp.update()

        for subscriber in subscribers:
            if (exclude and subscriber.imei not in exclude_list) or \
                    (include and subscriber.imei in include_list) or \
                    (not include and not exclude):
                self.send_message(sms_from, subscriber.msisdn, sms_text, is_silent)

    def stop_calls(self):
        subprocess.run(["bash", "-c", "rm -f /var/spool/asterisk/outgoing/*"])
        subprocess.run(["bash", "-c", 'asterisk -rx "hangup request all"'])
        subprocess.run(["bash", "-c", "rm -f /var/spool/asterisk/outgoing/*"])
        time.sleep(1)
        subprocess.run(["bash", "-c", 'asterisk -rx "hangup request all"'])
        subprocess.run(["bash", "-c", "rm -f /var/spool/asterisk/outgoing/*"])
        CallTimestamp.stop_calls()

    def clear_hlr(self):
        current_path = os.path.dirname(os.path.abspath(__file__))
        subprocess.run(f"bash -c {current_path}/max_stop".split())
        archive_path = f"{current_path}/../tmp/hlr_archive"
        subprocess.run(f"mkdir -p {archive_path}".split())
        archive_file_name = f"hlr_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        db_path = "/var/lib/osmocom"
        subprocess.run(f"mv {db_path}/hlr.db {archive_path}/{archive_file_name}".split())
        subprocess.run(f"bash -c {current_path}/max_start".split())

    def to_850(self):
        current_path = os.path.dirname(os.path.abspath(__file__))
        subprocess.run(f"bash -c {current_path}/850".split())

    def to_900(self):
        current_path = os.path.dirname(os.path.abspath(__file__))
        subprocess.run(f"bash -c {current_path}/900".split())

    def start(self):
        current_path = os.path.dirname(os.path.abspath(__file__))
        subprocess.run(f"bash -c {current_path}/max_start".split())

    def stop(self):
        self.stop_calls()
        current_path = os.path.dirname(os.path.abspath(__file__))
        subprocess.run(f"bash -c {current_path}/max_stop".split())

    def _process_logs(self, lines: List[str]):
        # pre filter
        lines = [line.strip() for line in lines if ("Started Osmocom" in line or
                                                    (
                                                            " New transaction" in line and "trans(CC" in line) or " new state " in line or
                                                    (" Paging expired" in line and "trans(CC" in line) or
                                                    (
                                                            "tid-255,PAGING) tx MNCC_REL_CNF" in line and "trans(CC" in line)) and
                 "tid-8" not in line
                 ]

        all_logs = {}
        logs = {}
        start_time = None

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

        if logs:
            all_logs[f"{start_time}-"] = logs

        return all_logs

    def calls_status(self):
        since = CallTimestamp.get_since_data()

        res = subprocess.run(["bash", "-c", f"journalctl -u osmo-msc --since='{since}'"], capture_output=True)
        lines = res.stdout.decode("UTF-8").split("\n")
        records = self._process_logs(lines)

        result_records = {}
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
        since = CallTimestamp.get_since_data()

        res = subprocess.run(["bash", "-c", f"journalctl -u osmo-msc --since='{since}'"], capture_output=True)
        lines = res.stdout.decode("UTF-8").split("\n")
        records = self._process_logs(lines)

        result_records = {}
        if len(records) > 0:
            last_record = records[list(records.keys())[-1]]

            for imsi, calls in last_record.items():
                result_records[imsi] = [imsi_call.status for imsi_call in calls.values()]
        return result_records

    def sms_statuses(self):
        since = SmsTimestamp.get_since_data()
        
        res = subprocess.run(["bash", "-c", f"journalctl -u osmo-msc --since='{since}' | grep 'stat:DELIVRD'"], capture_output=True)
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


if __name__ == '__main__':
    arg_parser = ArgumentParser(description="Sdr control", prog="sdr")
    subparsers = arg_parser.add_subparsers(help="action", dest="action", required=True)

    parser_show = subparsers.add_parser("show", help="show subscribers")
    parser_show.add_subparsers(help="check subscribers with silent calls and clear inaccessible ones",
                               dest="check_before").add_parser("check_before")

    parser_sms = subparsers.add_parser("sms", help="send sms")
    parser_sms.add_argument("sms_type", choices=["normal", "silent"], help="normal or silent")
    parser_sms.add_argument("send_from", help="sender, use ascii only")
    parser_sms.add_argument("message", help="message text")
    sms_subparsers = parser_sms.add_subparsers(help="send to", dest="sms_send_to", required=True)
    sms_subparsers.add_parser("all", help="send to all subscribers")
    sms_subparsers.add_parser("all_exclude", help="send to all subscribers exclude list")
    sms_subparsers.add_parser("include_list", help="send to subscribers from include list")
    sms_list_parser = sms_subparsers.add_parser("list", help="send to subscribers from list")
    sms_list_parser.add_argument("subscribers", help="subscribers list", type=str, nargs='+')

    parser_call = subparsers.add_parser("call", help="call to subscribers")
    parser_call.add_argument("call_from", help="caller, use numeric string [3-15] only", type=str)

    call_type_parsers = parser_call.add_subparsers(help="call type", dest="call_type", required=True)
    silent_parser = call_type_parsers.add_parser("silent", help="silent call")
    silent_subparsers = silent_parser.add_subparsers(help="call to", dest="call_to", required=True)
    silent_subparsers.add_parser("all", help="call to all subscribers")
    silent_subparsers.add_parser("all_exclude", help="call to all subscribers exclude list")
    silent_subparsers.add_parser("include_list", help="call to subscribers from include list")
    silent_call_list_parser = silent_subparsers.add_parser("list", help="call to subscribers from list")
    silent_call_list_parser.add_argument("subscribers", help="subscribers list", type=str, nargs='+')
    #
    voice_parser = call_type_parsers.add_parser("voice", help="voice call")
    voice_parser.add_argument("file_type", choices=["gsm", "mp3"], help="voice file type")
    voice_parser.add_argument("file", type=str, help="voice file path")

    voice_call_subparsers = voice_parser.add_subparsers(help="call to", dest="call_to", required=True)
    voice_call_subparsers.add_parser("all", help="call to all subscribers")
    voice_call_subparsers.add_parser("all_exclude", help="call to all subscribers exclude list")
    voice_call_subparsers.add_parser("include_list", help="call to subscribers from include list")
    voice_call_list_parser = voice_call_subparsers.add_parser("list", help="call to subscribers from list")
    voice_call_list_parser.add_argument("subscribers", help="subscribers list", type=str, nargs='+')

    subparsers.add_parser("stop_calls", help="stop all calls (restart asterisk)")
    subparsers.add_parser("clear_hlr", help="clear hlr base (with BS restart)")
    subparsers.add_parser("silent", help="silent call with speech")
    subparsers.add_parser("850", help="900 -> 850")
    subparsers.add_parser("900", help="850 -> 900")
    subparsers.add_parser("start", help="start Umbrella")
    subparsers.add_parser("stop", help="stop Umbrella")
    subparsers.add_parser("calls_status", help="get last call status")
    subparsers.add_parser("calls_status_filtered", help="get last filtered call status")
    subparsers.add_parser("sms_status", help="get last sms status")

    args = arg_parser.parse_args()

    sdr = Sdr(debug_output=True)

    action = args.action

    if action == "show":
        check_before = args.check_before is not None
        subscribers = sdr.get_subscribers(check_before=check_before, with_status=True)

        print("\n")
        print("===============================================================================================================")
        print("   msisdn       imsi               imei           last_ago     cell          ex  in  call status   sms status")
        print("===============================================================================================================")

        cells = {}
        cells_in = {}
        ops = {}
        current_path = os.path.dirname(os.path.abspath(__file__))
        with open(current_path + "/exclude_list") as f:
            exclude_list = [line.strip()[:14] for line in f.readlines()]
        with open(current_path + "/include_list") as f:
            include_list = [line.strip()[:14] for line in f.readlines()]

        call_records = sdr.calls_status_show()

        for subscriber in sorted(subscribers, key=lambda x: x.imei in include_list):
            call_status = call_records[subscriber.imsi][-1].name if subscriber.imsi in call_records else "-------------"
            print(f"   {subscriber.msisdn}        {subscriber.imsi}    {subscriber.imei} {subscriber.last_seen:>6}"
                  f"       {subscriber.cell}  {'+' if subscriber.imei in exclude_list else '-'}"
                  f"   {'+' if subscriber.imei in include_list else '-'}"
                  f"  {call_status:>13}"
                  f"  {subscriber.sms_status[-1] if len(subscriber.sms_status) > 0 else ''}")
            cells[subscriber.cell] = 1 if subscriber.cell not in cells else cells[subscriber.cell] + 1
            ops[subscriber.imsi[:5]] = 1 if subscriber.imsi[:5] not in ops else ops[subscriber.imsi[:5]] + 1

        print("===============================================================================================================")
        exclude_count = len([1 for subscriber in subscribers if subscriber.imei in exclude_list])
        include_count = len([1 for subscriber in subscribers if subscriber.imei in include_list])
        print(f"  Total: {len(subscribers)}  Exclude: {exclude_count}/{len(subscribers) - exclude_count}"
              f"  Include: {include_count}/{len(subscribers) - include_count}")
        print("\n\n  BS cells:")
        for cell, cnt in sorted(cells.items(), key=lambda x: x[0]):
            exclude_count = len(
                [1 for subscriber in subscribers if subscriber.imei in exclude_list and subscriber.cell == cell])
            include_count = len(
                [1 for subscriber in subscribers if subscriber.imei in include_list and subscriber.cell == cell])
            print(f"      {cell}: {cnt}/ex {exclude_count}/in {include_count}")

        print("\n\n  Ops by IMEI:")
        ops_names = {"25062": "Tinkoff", "25001": "MTS ", "25002": "Megafon", "25099": "Beeline", "25020": "Tele2",
                     "25011": "Yota", "40101": "KZ KarTel", "40177": "KZ Aktiv"}
        for op, cnt in sorted(ops.items(), key=lambda x: x[0]):
            print(f"      {op} {ops_names[op] if op in ops_names else '':10}: {cnt}")

    elif action == "sms":
        SmsTimestamp.update()
        sms_from = args.send_from
        text = args.message
        is_silent = args.sms_type == "silent"
        sms_send_to = args.sms_send_to
        if sms_send_to == "all":
            sdr.send_message_to_all(sms_from, text, is_silent=is_silent)
        elif sms_send_to == "all_exclude":
            sdr.send_message_to_all(sms_from, text, exclude=True, is_silent=is_silent)
        elif sms_send_to == "include_list":
            sdr.send_message_to_all(sms_from, text, include=True, is_silent=is_silent)
        elif sms_send_to == "list":
            for subscriber in args.subscribers:
                sdr.send_message(sms_from, subscriber, text, is_silent=is_silent)

    elif action == "call":

        call_type = args.call_type
        file_type = args.file_type if hasattr(args, "file_type") else None
        call_to = args.call_to
        call_from = args.call_from
        voice_file = args.file if hasattr(args, "file") else None

        call_type = CallType.SILENT if call_type == "silent" else (CallType.GSM if file_type == "gsm" else CallType.MP3)

        if call_to == "all":
            sdr.call_to_all(call_type, voice_file, call_from)
        elif call_to == "all_exclude":
            sdr.call_to_all(call_type, voice_file, call_from, exclude=True)
        elif call_to == "include_list":
            sdr.call_to_all(call_type, voice_file, call_from, include=True)
        elif call_to == "list":
            for subscriber in args.subscribers:
                sdr.call(call_type, subscriber, call_from, voice_file)
    elif action == "stop_calls":
        sdr.stop_calls()
    elif action == "clear_hlr":
        sdr.clear_hlr()
    elif action == "silent":
        sdr.silent_call()
    elif action == "850":
        sdr.to_850()
    elif action == "900":
        sdr.to_900()
    elif action == "start":
        sdr.start()
    elif action == "stop":
        sdr.stop()
    elif action == "calls_status":
        subscribers = sdr.get_subscribers(with_status=True)
        prefix = "                              "
        prefix_end = "=============================="

        for subscriber in subscribers:
            print(f"{subscriber.imei}/{subscriber.imsi}:")
            for call in subscriber.calls_status:
                print(f"{prefix}{call}")
            print(prefix_end)
    elif action == "calls_status_filtered":
        results = sdr.calls_status()
        pprint.pprint(results)
    elif action == "sms_status":
        pprint.pprint(sdr.sms_statuses())
