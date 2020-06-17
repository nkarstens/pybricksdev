# SPDX-License-Identifier: MIT
# Copyright (c) 2019-2020 The Pybricks Authors

import asyncio
import io
import struct
import sys
from collections import namedtuple
from tqdm import tqdm
import logging
from pybricksdev.ble import BLERequestsConnection


def sum_complement(fw, max_size):
    """Calculates the checksum of a firmware file using the sum complement
    method of adding each 32-bit word and the returning the two's complement
    as the checksum.

    Arguments:
        fw (file):
            The firmware file (a binary buffer - e.g. a file opened in 'rb' mode)
        max_size (int):
            The maximum size of the firmware file.

    Returns:
        int: The correction needed to make the checksum of the file == 0.
    """
    checksum = 0
    size = 0

    while True:
        word = fw.read(4)
        if not word:
            break
        checksum += struct.unpack('I', word)[0]
        size += 4

    if size > max_size:
        print('firmware + main.mpy is too large"', file=sys.stderr)
        exit(1)

    for _ in range(size, max_size, 4):
        checksum += 0xffffffff

    checksum &= 0xffffffff
    correction = checksum and (1 << 32) - checksum or 0

    return correction


def create_firmware(base, mpy, metadata):
    """Creates a firmware blob from base firmware and main.mpy file.

    Arguments:
        base (bytes):
            base firmware binary blob
        mpy (bytes):
            main.mpy binary blob
        metadata (dict):
            firmware metadata

    Returns:
        bytes: composite binary blob with correct padding and checksum
    """
    # start with base firmware binary blob
    firmware = bytearray(base)
    # pad with 0s until user-mpy-offset
    firmware.extend(
        0 for _ in range(metadata['user-mpy-offset'] - len(firmware)))
    # append 32-bit little-endian main.mpy file size
    firmware.extend(struct.pack('<I', len(mpy)))
    # append main.mpy file
    firmware.extend(mpy)
    # pad with 0s to align to 4-byte boundary
    firmware.extend(0 for _ in range(-len(firmware) % 4))

    # append 32-bit little-endian checksum
    if metadata['checksum-type'] == "sum":
        firmware.extend(
            struct.pack(
                '<I',
                sum_complement(io.BytesIO(firmware),
                               metadata['max-firmware-size'] - 4)))
    else:
        print(f'Unknown checksum type "{metadata["checksum-type"]}"',
              file=sys.stderr)
        exit(1)

    return firmware


HUB_NAMES = {
    0x40: 'Move Hub',
    0x41: 'City Hub',
    0x80: 'Control+ Hub'
}


class BootloaderRequest():
    """Bootloader request structure."""

    def __init__(self, command, name, request_format, data_format, request_confirm):
        self.command = command
        self.ReplyClass = namedtuple(name, request_format)
        self.data_format = data_format
        self.reply_len = struct.calcsize(data_format)
        if request_confirm:
            self.reply_len += 1

    def make_request(self, payload=None):
        request = bytearray((self.command,))
        if payload is not None:
            request += payload
        return request

    def parse_reply(self, reply):
        if reply[0] == self.command:
            return self.ReplyClass(*struct.unpack(self.data_format, reply[1:]))
        else:
            raise ValueError("Unknown message: {0}".format(reply))


# Create the static instances
BootloaderRequest.ERASE_FLASH = BootloaderRequest(
    0x11, 'Erase', ['result'], '<B', True
)
BootloaderRequest.PROGRAM_FLASH_BARE = BootloaderRequest(
    0x22, 'Flash', [], '', False
)
BootloaderRequest.PROGRAM_FLASH = BootloaderRequest(
    0x22, 'Flash', ['checksum', 'count'], '<BI', True
)
BootloaderRequest.START_APP = BootloaderRequest(
    0x33, 'Start', [], '', True
)
BootloaderRequest.INIT_LOADER = BootloaderRequest(
    0x44, 'Init', ['result'], '<B', True
)
BootloaderRequest.GET_INFO = BootloaderRequest(
    0x55, 'Info', ['version', 'start_addr', 'end_addr', 'type_id'], '<iIIB', True
)
BootloaderRequest.GET_CHECKSUM = BootloaderRequest(
    0x66, 'Checksum', ['checksum'], '<B', True
)
BootloaderRequest.GET_FLASH_STATE = BootloaderRequest(
    0x77, 'State', ['level'], '<B', True
)
BootloaderRequest.DISCONNECT = BootloaderRequest(
    0x88, 'Disconnect', [], '', True
)


class BootloaderConnection(BLERequestsConnection):
    """Connect to Powered Up Hub Bootloader and update firmware."""

    def __init__(self):
        """Initialize the BLE Connection for Bootloader service."""
        super().__init__('00001626-1212-efde-1623-785feabcd123')

    async def bootloader_request(self, request, payload=None, delay=0.05):
        """Sends a message to the bootloader and awaits corresponding reply."""

        # Get message command and expected reply length
        self.prepare_reply(request.reply_len)

        # Write message
        data = request.make_request(payload)
        await self.write(data, delay)

        # If we expect a reply, await for it
        if request.reply_len > 0:
            self.logger.debug("Awaiting reply of {0}".format(self.reply_len))
            reply = await self.wait_for_reply()
            return request.parse_reply(reply)

    async def flash(self, firmware, metadata, delay):

        # Firmware information
        firmware_io = io.BytesIO(firmware)
        firmware_size = len(firmware)

        print("Getting device info.")
        info = await self.bootloader_request(BootloaderRequest.GET_INFO)
        self.logger.debug(info)

        # Check hub ID
        if info.type_id != metadata['device-id']:
            await self.disconnect()
            raise RuntimeError(
                "This firmware {0}, but we are connected to {1}.".format(
                    HUB_NAMES[metadata['device-id']], HUB_NAMES[info.type_id]
                )
            )

        # TODO: Use write with response on CityHub

        print("Erasing flash.")
        response = await self.bootloader_request(BootloaderRequest.ERASE_FLASH)
        self.logger.debug(response)

        print('Validating size.')
        response = await self.bootloader_request(
            request=BootloaderRequest.INIT_LOADER,
            payload=struct.pack('<I', firmware_size)
        )
        self.logger.debug(response)

        count = 0
        max_payload_size = 32

        print('flashing firmware...')

        # Maintain progress using tqdm
        with tqdm(total=firmware_size, unit='B', unit_scale=True) as pbar:

            # Start writing at the start address
            addr = info.start_addr

            # Repeat until the whole firmware has been processed
            while firmware_io.tell() != firmware_size:

                count += 1
                if count % 1000 == 0:
                    try:
                        response = await asyncio.wait_for(self.bootloader_request(BootloaderRequest.GET_CHECKSUM), 2)
                        print(response)
                    except (asyncio.exceptions.TimeoutError, ValueError):
                        print("Got stuck, try disconnect")
                        break

                # The write address is the starting address plus
                # how much has been written already
                address = info.start_addr + firmware_io.tell()

                # Read the firmware chunk to be sent
                payload = firmware_io.read(max_payload_size)

                # Check if this is the last chunk to be sent
                if firmware_io.tell() == firmware_size:
                    # If so, request flash with confirmation request.
                    request = BootloaderRequest.PROGRAM_FLASH
                else:
                    # Otherwise, do not wait for confirmation.
                    request = BootloaderRequest.PROGRAM_FLASH_BARE

                # Pack the data in the expected format
                data = struct.pack('<BI' + 'B' * len(payload), len(payload) + 4, address, *payload)
                response = await self.bootloader_request(request, data, delay)
                self.logger.debug(response)
                pbar.update(len(payload))


async def flash_firmware(address, blob, metadata, delay):
    updater = BootloaderConnection()
    updater.logger.setLevel(logging.DEBUG)
    await updater.connect(address)
    await updater.flash(blob, metadata, delay/1000)
    await updater.disconnect()
