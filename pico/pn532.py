"""
This MicroPython module lets you communicate with the PN532 nfc/rfid module over I2C,
It also covers auth/read/write on a mifare card.
"""

from time import sleep_ms, ticks_ms, ticks_diff  # pyright: ignore

_ACK = b"\x00\x00\xFF\x00\xFF\x00"
_NACK = b"\x00\x00\xFF\xFF\x00\x00"
_PN532_ADDR = 0x24
_PREAMBLE = 0x00
_POSTAMBLE = 0x00
_START_CODE_1 = 0x00
_START_CODE_2 = 0xFF
_TFI_H2M = 0xD4
_TFI_M2H = 0xD5

_CMD_GetFirmwareVersion = 0x02
_CMD_GetGeneralStatus = 0x04
_CMD_SAM_CONFIGURATION = 0x14
_CMD_PowerDown = 0x16
_CMD_InListPassiveTarget = 0x4A
_CMD_InDataExchange = 0x40

_MIFARE_AUTH_A = 0x60
_MIFARE_AUTH_B = 0x61
_MIFARE_READ = 0x30
_MIFARE_WRITE = 0xA0
_MIFARE_INCREMENT = 0xC1
_MIFARE_DECREMENT = 0xC0

A = 0x00
B = 0x01
DEFAULT_KEYA = DEFAULT_KEYB = [0xFF] * 6


class PN532_I2C:
    """
    The I2C driver class
    """

    def __init__(self, i2c, debug=False):
        self.i2c = i2c
        self.debug = debug
        self.pow_down = False

    def write_rawdata(self, data):
        """
        writes data which should be of type bytes and return number of ack received
        """
        return self.i2c.writeto(_PN532_ADDR, data)

    def read_rawdata(self, n=6, retries=6, retry_delay=10):
        """
        Try to read n bytes for a certain amout of times: if it fails it raises an error,
        if it succeed it ruturn a bytes object
        """
        for _ in range(retries):
            try:
                return self.i2c.readfrom(_PN532_ADDR, n)
            except OSError:
                if self.debug:
                    print("reading failed, retrying ...")
                sleep_ms(retry_delay)
        raise OSError("Read failed after retries")

    def read_frame(self, n=32):
        """
        Read a frame of data with max length of n, if the operation fails of the returned frame is invalid it raises an error
        otherwise it returns the bytes from the TFI (included) to the POSTAMBLE (included)
        """
        if self.debug:
            print("Reading frame ...")
        res = self.read_rawdata(n + 8)
        if self.debug:
            hexstr = res.hex(" ")
            print(f" received frame: {hexstr}\n")
        if res[0] != 0x01:
            raise OSError("Not ready")
        if res[1] != _PREAMBLE or res[2] != _START_CODE_1 or res[3] != _START_CODE_2:
            raise OSError("Invalid response")
        l = res[4]
        lcs = res[5]
        if (l + lcs) & 0xFF != 0x00:
            raise OSError("Length checksum doesn't match")
        if n < l:
            raise OSError("Frame not read completely")
        if res[6] != _TFI_M2H or res[l + 7] != 0x00:
            raise OSError("Invalid response")
        if sum(res[6 : l + 7]) & 0xFF != 0x00:
            raise OSError("Data checksum doesn't match")
        return res[7 : l + 6]

    def wait_ready(self, timeout=1000, retry_delay=5):
        """
        Waits a certain amount for the PN532 to return a ready byte,
        if received returns True else if timeout expire reture False
        """
        if self.debug:
            print("Waiting for device to be ready ...")
        timestamp = ticks_ms()
        while timeout is None or ticks_diff(ticks_ms(), timestamp) < timeout:
            try:
                sleep_ms(retry_delay)
                r = self.read_rawdata(1)
                if r and r[0] == 0x01:
                    if self.debug:
                        print("Device ready ")
                    return True
            except OSError:
                pass
                if self.debug:
                    print("Waiting timeout exceeded")
                return False

    def write_cmd(self, cmd, params=None):
        """
        Write a command to the PN532 with given parameters;
        parameters should be given as an array of intergers between 0 and 255
        if the write fails or the PN532 doesn't return an ACK signal it raises an error
        """
        data = bytes([_TFI_H2M, cmd & 0xFF])
        if params is not None:
            data += bytes([p & 0xFF for p in params])

        l = len(data)
        lcs = 0xFF - l + 1
        dcs = (0xFF - sum(data) + 1) & 0xFF
        frame = (
            bytes([_PREAMBLE, _START_CODE_1, _START_CODE_2, l, lcs])
            + data
            + bytes([dcs, _POSTAMBLE])
        )
        if self.debug:
            hexstr = frame.hex(" ")
            print(f" Frame sent :{hexstr}")
        self.write_rawdata(frame)
        if not self.wait_ready():
            raise OSError("Device not ready")
        ack = self.read_rawdata(len(_ACK) + 1)
        if ack != b"\x01" + _ACK:
            raise OSError("No ack received")

    def firmware_version(self):
        """
        Returns the firmware version of the PN532 as an array having IC ver, Firmware ver, Frimware rev, support values
        Refer to the PN532 user-guide for more details
        """
        if self.debug:
            print("Getting firmware version ...")
        self.write_cmd(_CMD_GetFirmwareVersion)
        if not self.wait_ready():
            raise OSError("Device not ready")
        data = self.read_frame()
        if data[0] != _CMD_GetFirmwareVersion + 1:
            raise OSError("Invalid response")
        if self.debug:
            hexstr = data[1:].hex(" ")
            print(f"Firmware version: {hexstr}")
        return data[1:]

    def general_status(self):
        """
        Returns the general status of the PN532 as an array which contains err code, RF field, number of targets,
        and depending on the number of targets either nothing or one/two tuples (tg, sens_res, sel_res, NFCIDLength)
        """
        if self.debug:
            print("Getting general status ...")
        self.write_cmd(_CMD_GetGeneralStatus)
        if not self.wait_ready():
            raise OSError("Device not ready")
        res = self.read_frame()
        if res[0] != _CMD_GetGeneralStatus + 1:
            raise OSError("Invalid response")
        tg = res[3]
        res = list(res)
        if tg == 0:
            res = res[1:]
        elif tg == 1:
            res = res[1:4] + [(res[4], res[5], res[6], res[7])] + res[8:]
        else:
            res = (
                res[1:4]
                + [(res[4], res[5], res[6], res[7]), (res[8], res[9], res[10], res[11])]
                + res[12:]
            )
        if self.debug:
            print(f"General status: {res}")
        return res

    def set_mode(self, mode=0x01):
        """
        Sets the SAM_Configuration mode to 1 to initiate PCD mode, raises an error if it failed
        refers to the PN532 user-guide for more details about modes
        """
        if self.debug:
            print(f"Setting device mode to {mode} ...")
        self.write_cmd(_CMD_SAM_CONFIGURATION, [mode, 0x00])
        if not self.wait_ready():
            raise OSError("Device not ready")
        res = self.read_frame()
        if res[0] != _CMD_SAM_CONFIGURATION + 1:
            raise OSError("Invalid response")

    def list_passive_target(self, timeout=1000):
        """
        Listen for a given amount of time for passive targets
        if none are detected within timeout return an empty list
        else return an array that contains the target logical number, SENS_RES, SEL_RES, and the UID of the detected card
        UID is returned as an array of integers where each element represent a byte
        raises an error if the operation fails or more than one target is detected
        **currently it only supports ISO/IEC14443 Type A targets
        """
        if self.debug:
            print("Listing Passive Targets ...")
        self.write_cmd(_CMD_InListPassiveTarget, [0x01, 0x00])
        if not self.wait_ready(timeout):
            if self.debug:
                print("No Device detected")
            self.abort_cmd()
            return []
        res = self.read_frame()
        if res[0] != _CMD_InListPassiveTarget + 1:
            raise OSError("Invalid response")
        if res[1] != 0x01:
            raise OSError("More than one card detected")
        uid_len = res[6]
        if uid_len > 7:
            raise OSError("The card's UID is too long")
        return [res[2], int.from_bytes(res[3:5]), res[5], list(res[7 : 7 + uid_len])]

    def mifare_classic_auth(self, uid, tg, key, key_type, block):
        """
        Send a MIFARE authentification command to the card with a given UID and tg logical number using the given key and key type on the given block
        key and uid should be given as arrays of integers where each element represent a byte
        raises an error if the operation fails
        """
        if self.debug:
            print(f"Mifare authentification with key {key} on block {block} ...")
        param = [0] * 13
        param[0] = tg
        if key_type == A:
            param[1] = _MIFARE_AUTH_A
        elif key_type == B:
            param[1] = _MIFARE_AUTH_B
        else:
            raise OSError("Invalid key type")
        param[2] = block
        param[3:9] = key
        param[9:13] = uid
        self.write_cmd(_CMD_InDataExchange, param)
        if not self.wait_ready():
            raise OSError("Device not ready")
        res = self.read_frame(3)
        if res[0] != _CMD_InDataExchange + 1:
            raise OSError("Invalid response")
        if res[1] & 0x3F != 0x00:
            raise OSError("Command failed")

    def mifare_classic_read(self, tg, block):
        """
        Send a MIFARE read command to the card with the given tg logical number to read the given block
        returns a bytes object containing the 16 bytes read from the block
        raises an error if the operation fails
        """
        if self.debug:
            print(f"Reading from block {block} ...")
        self.write_cmd(_CMD_InDataExchange, [tg, _MIFARE_READ, block])
        if not self.wait_ready():
            raise OSError("Device not ready")
        res = self.read_frame()
        if res[0] != _CMD_InDataExchange + 1:
            raise OSError("Invalid response")
        if res[1] & 0x3F != 0x00:
            raise OSError("Command failed")
        if self.debug:
            print(f"Data read: {res[2:18]}")
        return res[2:18]

    def mifare_classic_write(self, tg, block, data):
        """
        Send a MIFARE write command to the card with the given tg logical number to write the given block
        data should be a bytes object containing up to 16 bytes to write to the block
        raises an error if the operation fails or the data size is greater than 16
        """
        if self.debug:
            print(f"Writing to block {block} ...")
        param = [0] * 19
        param[0] = tg
        param[1] = _MIFARE_WRITE
        param[2] = block
        if len(data) > 16:
            raise OSError("data length exceeds 16 bytes")
        else:
            param[3:19] = list(data) + [0x00] * (16 - len(data))
        self.write_cmd(_CMD_InDataExchange, param)
        if not self.wait_ready():
            raise OSError("Device not ready")
        res = self.read_frame()
        if res[0] != _CMD_InDataExchange + 1:
            raise OSError("Invalid response")
        if res[1] & 0x3F != 0x00:
            raise OSError("Command failed")

    def power_down(self, wakeup_causes=0x88):
        """
        Put the device into low power mode and set the wakeup causes
        Refers to the PN532 user-guide for more details about wakeup causes
        """
        if self.debug:
            print("Putting Device into low power mode ...")
        self.write_cmd(_CMD_PowerDown, [wakeup_causes])
        if not self.wait_ready():
            raise OSError("Device not ready")
        res = self.read_frame()
        if res[0] != _CMD_PowerDown + 1:
            raise OSError("Invalid response")
        if res[1] & 0x3F != 0x00:
            raise OSError("Command failed")
        self.pow_down = True

    def wakeup(self):
        """
        Wakeup the device by sending a dummy I2C request (ACK)
        """
        if self.debug:
            print("Waking up device ...")
        self.write_rawdata(_ACK)
        sleep_ms(50)
        self.pow_down = False

    def abort_cmd(self):
        """
        Abort the current command by sending an ACK
        """
        if self.debug:
            print("Sending ACK to abort ...")
        self.write_rawdata(_ACK)
