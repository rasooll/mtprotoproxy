#!/usr/bin/env python3

import asyncio
import socket
import urllib.parse
import urllib.request
import collections
import time
import hashlib
import random
import binascii

try:
    from Crypto.Cipher import AES
    from Crypto.Util import Counter

    def create_aes_ctr(key, iv):
        ctr = Counter.new(128, initial_value=iv)
        return AES.new(key, AES.MODE_CTR, counter=ctr)

    def create_aes_cbc(key, iv):
        return AES.new(key, AES.MODE_CBC, iv)

except ImportError:
    print("Failed to find pycrypto, using slow AES version", flush=True)
    import pyaes

    def create_aes_ctr(key, iv):
        ctr = pyaes.Counter(iv)
        return pyaes.AESModeOfOperationCTR(key, ctr)

    def create_aes_cbc(key, iv):
        class EncryptorAdapter:
            def __init__(self, mode):
                self.mode = mode

            def encrypt(self, data):
                encrypter = pyaes.Encrypter(self.mode, pyaes.PADDING_NONE)
                return encrypter.feed(data) + encrypter.feed()

            def decrypt(self, data):
                decrypter = pyaes.Decrypter(self.mode, pyaes.PADDING_NONE)
                return decrypter.feed(data) + decrypter.feed()

        mode = pyaes.AESModeOfOperationCBC(key, iv)
        return EncryptorAdapter(mode)


import config
PORT = getattr(config, "PORT")
USERS = getattr(config, "USERS")

# load advanced settings
PREFER_IPV6 = getattr(config, "PREFER_IPV6", False)
# disables tg->client trafic reencryption, faster but less secure
FAST_MODE = getattr(config, "FAST_MODE", True)
STATS_PRINT_PERIOD = getattr(config, "STATS_PRINT_PERIOD", 600)
READ_BUF_SIZE = getattr(config, "READ_BUF_SIZE", 4096)
AD_TAG = bytes.fromhex(getattr(config, "AD_TAG", ""))

TG_DATACENTER_PORT = 443

TG_DATACENTERS_V4 = [
    "149.154.175.50", "149.154.167.51", "149.154.175.100",
    "149.154.167.91", "149.154.171.5"
]

TG_DATACENTERS_V6 = [
    "2001:b28:f23d:f001::a", "2001:67c:04e8:f002::a", "2001:b28:f23d:f003::a",
    "2001:67c:04e8:f004::a", "2001:b28:f23f:f005::a"
]

TG_MIDDLE_PROXIES_V4 = [
    ("149.154.175.50", 8888), ("149.154.162.38", 80), ("149.154.175.100", 8888),
    ("91.108.4.136", 8888), ("91.108.56.181", 8888)
]


USE_MIDDLE_PROXY = (len(AD_TAG) == 16)

PROXY_SECRET = bytes.fromhex(
    "c4f9faca9678e6bb48ad6c7e2ce5c0d24430645d554addeb55419e034da62721" +
    "d046eaab6e52ab14a95a443ecfb3463e79a05a66612adf9caeda8be9a80da698" +
    "6fb0a6ff387af84d88ef3a6413713e5c3377f6e1a3d47d99f5e0c56eece8f05c" +
    "54c490b079e31bef82ff0ee8f2b0a32756d249c5f21269816cb7061b265db212"
)

SKIP_LEN = 8
PREKEY_LEN = 32
KEY_LEN = 32
IV_LEN = 16
HANDSHAKE_LEN = 64
MAGIC_VAL_POS = 56

MAGIC_VAL_TO_CHECK = b'\xef\xef\xef\xef'

CBC_PADDING = 16
PADDING_FILLER = b"\x04\x00\x00\x00"

MIN_MSG_LEN = 12
MAX_MSG_LEN = 2 ** 24

global_my_ip = None


def init_stats():
    global stats
    stats = {user: collections.Counter() for user in USERS}


def update_stats(user, connects=0, curr_connects_x2=0, octets=0):
    global stats

    if user not in stats:
        stats[user] = collections.Counter()

    stats[user].update(connects=connects, curr_connects_x2=curr_connects_x2,
                       octets=octets)


class CryptoWrappedStreamReader:
    def __init__(self, stream, decryptor, block_size=1):
        self.stream = stream
        self.decryptor = decryptor
        self.block_size = block_size
        self.buf = bytearray()

    def __getattr__(self, attr):
        return getattr(self.stream, attr)

    async def read(self, n):
        if self.buf:
            ret = self.buf
            self.buf.clear()
            return ret
        else:
            readed = await self.stream.read(n)

            needed_till_full_block = -len(readed) % self.block_size
            if needed_till_full_block > 0:
                readed += self.stream.readexactly(needed_till_full_block)
            return self.decryptor.decrypt(readed)

    async def readexactly(self, n):
        if n > len(self.buf):
            to_read = n - len(self.buf)
            needed_till_full_block = -to_read % self.block_size

            to_read_block_aligned = to_read + needed_till_full_block
            data = await self.stream.readexactly(to_read_block_aligned)
            self.buf += self.decryptor.decrypt(data)

        ret = bytes(self.buf[:n])
        self.buf = self.buf[n:]
        return ret


class CryptoWrappedStreamWriter:
    def __init__(self, stream, encryptor, block_size=1):
        self.stream = stream
        self.encryptor = encryptor
        self.block_size = block_size

    def __getattr__(self, attr):
        return getattr(self.stream, attr)

    def write(self, data):
        if len(data) % self.block_size != 0:
            print("BUG: writing %d bytes not aligned to block size %d" % (
                len(data), self.block_size))
            return 0
        q = self.encryptor.encrypt(data)
        return self.stream.write(q)


class MTProtoFrameStreamReader:
    def __init__(self, stream, seq_no=0):
        self.stream = stream
        self.seq_no = seq_no

    def __getattr__(self, attr):
        return getattr(self.stream, attr)

    async def read(self, buf_size):
        msg_len_bytes = await self.stream.readexactly(4)
        msg_len = int.from_bytes(msg_len_bytes, "little")
        # skip paddings
        while msg_len == 4:
            msg_len_bytes = await self.stream.readexactly(4)
            msg_len = int.from_bytes(msg_len_bytes, "little")

        len_is_impossible = (msg_len % len(PADDING_FILLER) != 0)
        if not MIN_MSG_LEN <= msg_len <= MAX_MSG_LEN or len_is_impossible:
            print("msg_len is bad, closing connection", msg_len)
            self.stream.feed_eof()
            return b""

        msg_seq_bytes = await self.stream.readexactly(4)
        msg_seq = int.from_bytes(msg_seq_bytes, "little", signed=True)
        if msg_seq != self.seq_no:
            print("unexpected seq_no")
            self.stream.feed_eof()
            return b""

        self.seq_no += 1

        data = await self.stream.readexactly(msg_len - 4 - 4 - 4)
        checksum_bytes = await self.stream.readexactly(4)
        checksum = int.from_bytes(checksum_bytes, "little")

        computed_checksum = binascii.crc32(msg_len_bytes + msg_seq_bytes + data)
        if computed_checksum != checksum:
            self.stream.feed_eof()
            return b""
        return data


class MTProtoCompactFrameStreamReader:
    def __init__(self, stream):
        self.stream = stream

    def __getattr__(self, attr):
        return getattr(self.stream, attr)

    async def read(self, buf_size):
        msg_len_bytes = await self.stream.readexactly(1)
        msg_len = int.from_bytes(msg_len_bytes, "little")

        if msg_len >= 0x80:
            msg_len -= 0x80

        if msg_len == 0x7f:
            msg_len_bytes = await self.stream.readexactly(3)
            msg_len = int.from_bytes(msg_len_bytes, "little")

        msg_len *= 4

        data = await self.stream.readexactly(msg_len)

        return data


class MTProtoCompactFrameStreamWriter:
    def __init__(self, stream, seq_no=0):
        self.stream = stream
        self.seq_no = seq_no

    def __getattr__(self, attr):
        return getattr(self.stream, attr)

    def write(self, data):
        SMALL_PKT_BORDER = 0x7f
        LARGE_PKT_BORGER = 256 ** 3

        if len(data) % 4 != 0:
            print("BUG: MTProtoFrameStreamWriter attempted to send msg with len %d" % len(msg))
            return 0

        len_div_four = len(data) // 4

        if len_div_four < SMALL_PKT_BORDER:
            return self.stream.write(bytes([len_div_four]) + data)
        elif len_div_four < LARGE_PKT_BORGER:
            return self.stream.write(b'\x7f' + bytes(int.to_bytes(len_div_four, 3, 'little')) +
                                     data)
        else:
            print("Attempted to send too large pkt len =", len(data))
            return 0


class MTProtoFrameStreamWriter:
    def __init__(self, stream, seq_no=0):
        self.stream = stream
        self.seq_no = seq_no

    def __getattr__(self, attr):
        return getattr(self.stream, attr)

    def write(self, msg):
        len_bytes = int.to_bytes(len(msg) + 4 + 4 + 4, 4, "little")
        seq_bytes = int.to_bytes(self.seq_no, 4, "little", signed=True)
        self.seq_no += 1

        msg_without_checksum = len_bytes + seq_bytes + msg
        checksum = int.to_bytes(binascii.crc32(msg_without_checksum), 4, "little")

        full_msg = msg_without_checksum + checksum
        padding = PADDING_FILLER * ((-len(full_msg) % CBC_PADDING) // len(PADDING_FILLER))

        return self.stream.write(full_msg + padding)


class ProxyReqStreamReader:
    def __init__(self, stream):
        self.stream = stream

    def __getattr__(self, attr):
        return getattr(self.stream, attr)

    async def read(self, msg):
        RPC_PROXY_ANS = b"\x0d\xda\x03\x44"
        RPC_CLOSE_EXT = b"\xa2\x34\xb6\x5e"

        data = await self.stream.read(1)

        if len(data) < 4:
            return b""

        ans_type, ans_flags, conn_id, conn_data = data[:4], data[4:8], data[8:16], data[16:]
        if ans_type == RPC_CLOSE_EXT:
            self.feed_eof()
            return b""

        if ans_type != RPC_PROXY_ANS:
            print("ans_type != RPC_PROXY_ANS", ans_type)
            return b""

        return conn_data


class ProxyReqStreamWriter:
    def __init__(self, stream):
        self.stream = stream

    def __getattr__(self, attr):
        return getattr(self.stream, attr)

    def write(self, msg):
        RPC_PROXY_REQ = b"\xee\xf1\xce\x36"
        FLAGS = b"\x08\x10\x02\x40"
        OUT_CONN_ID = bytearray([random.randrange(0, 256) for i in range(8)])
        REMOTE_IP_PORT = b"A" * 20
        OUR_IP_PORT = b"B" * 20
        EXTRA_SIZE = b"\x18\x00\x00\x00"
        PROXY_TAG = b"\xae\x26\x1e\xdb"
        FOUR_BYTES_ALIGNER = b"\x00\x00\x00"

        if len(msg) % 4 != 0:
            print("BUG: attempted to send msg with len %d" % len(msg))
            return 0

        full_msg = bytearray()
        full_msg += RPC_PROXY_REQ + FLAGS + OUT_CONN_ID + REMOTE_IP_PORT
        full_msg += OUR_IP_PORT + EXTRA_SIZE + PROXY_TAG
        full_msg += bytes([len(AD_TAG)]) + AD_TAG + FOUR_BYTES_ALIGNER
        full_msg += msg

        return self.stream.write(full_msg)


async def handle_handshake(reader, writer):
    handshake = await reader.readexactly(HANDSHAKE_LEN)

    for user in USERS:
        secret = bytes.fromhex(USERS[user])

        dec_prekey_and_iv = handshake[SKIP_LEN:SKIP_LEN+PREKEY_LEN+IV_LEN]
        dec_prekey, dec_iv = dec_prekey_and_iv[:PREKEY_LEN], dec_prekey_and_iv[PREKEY_LEN:]
        dec_key = hashlib.sha256(dec_prekey + secret).digest()
        decryptor = create_aes_ctr(key=dec_key, iv=int.from_bytes(dec_iv, "big"))

        enc_prekey_and_iv = handshake[SKIP_LEN:SKIP_LEN+PREKEY_LEN+IV_LEN][::-1]
        enc_prekey, enc_iv = enc_prekey_and_iv[:PREKEY_LEN], enc_prekey_and_iv[PREKEY_LEN:]
        enc_key = hashlib.sha256(enc_prekey + secret).digest()
        encryptor = create_aes_ctr(key=enc_key, iv=int.from_bytes(enc_iv, "big"))

        decrypted = decryptor.decrypt(handshake)

        check_val = decrypted[MAGIC_VAL_POS:MAGIC_VAL_POS+4]
        if check_val != MAGIC_VAL_TO_CHECK:
            continue

        dc_idx = abs(int.from_bytes(decrypted[60:62], "little", signed=True)) - 1
        if dc_idx == 0:
            continue

        reader = CryptoWrappedStreamReader(reader, decryptor)
        writer = CryptoWrappedStreamWriter(writer, encryptor)
        return reader, writer, user, dc_idx, enc_key + enc_iv
    return False


async def do_direct_handshake(dc_idx, dec_key_and_iv=None):
    RESERVED_NONCE_FIRST_CHARS = [b"\xef"]
    RESERVED_NONCE_BEGININGS = [b"\x48\x45\x41\x44", b"\x50\x4F\x53\x54",
                                b"\x47\x45\x54\x20", b"\xee\xee\xee\xee"]
    RESERVED_NONCE_CONTINUES = [b"\x00\x00\x00\x00"]

    if PREFER_IPV6:
        if not 0 <= dc_idx < len(TG_DATACENTERS_V6):
            return False
        dc = TG_DATACENTERS_V6[dc_idx]
    else:
        if not 0 <= dc_idx < len(TG_DATACENTERS_V4):
            return False
        dc = TG_DATACENTERS_V4[dc_idx]

    try:
        reader_tgt, writer_tgt = await asyncio.open_connection(dc, TG_DATACENTER_PORT)
    except ConnectionRefusedError as E:
        return False
    except OSError as E:
        return False

    while True:
        rnd = bytearray([random.randrange(0, 256) for i in range(HANDSHAKE_LEN)])
        if rnd[:1] in RESERVED_NONCE_FIRST_CHARS:
            continue
        if rnd[:4] in RESERVED_NONCE_BEGININGS:
            continue
        if rnd[4:8] in RESERVED_NONCE_CONTINUES:
            continue
        break

    rnd[MAGIC_VAL_POS:MAGIC_VAL_POS+4] = MAGIC_VAL_TO_CHECK

    if dec_key_and_iv:
        rnd[SKIP_LEN:SKIP_LEN+KEY_LEN+IV_LEN] = dec_key_and_iv[::-1]

    rnd = bytes(rnd)

    dec_key_and_iv = rnd[SKIP_LEN:SKIP_LEN+KEY_LEN+IV_LEN][::-1]
    dec_key, dec_iv = dec_key_and_iv[:KEY_LEN], dec_key_and_iv[KEY_LEN:]
    decryptor = create_aes_ctr(key=dec_key, iv=int.from_bytes(dec_iv, "big"))

    enc_key_and_iv = rnd[SKIP_LEN:SKIP_LEN+KEY_LEN+IV_LEN]
    enc_key, enc_iv = enc_key_and_iv[:KEY_LEN], enc_key_and_iv[KEY_LEN:]
    encryptor = create_aes_ctr(key=enc_key, iv=int.from_bytes(enc_iv, "big"))

    rnd_enc = rnd[:MAGIC_VAL_POS] + encryptor.encrypt(rnd)[MAGIC_VAL_POS:]

    writer_tgt.write(rnd_enc)
    await writer_tgt.drain()

    reader_tgt = CryptoWrappedStreamReader(reader_tgt, decryptor)
    writer_tgt = CryptoWrappedStreamWriter(writer_tgt, encryptor)

    return reader_tgt, writer_tgt


def get_middleproxy_aes_key_and_iv(nonce_srv, nonce_clt, clt_ts, srv_ip, clt_port, purpose,
                                   clt_ip, srv_port, middleproxy_secret, clt_ipv6=None,
                                   srv_ipv6=None):

    s = bytearray()
    s += nonce_srv + nonce_clt + clt_ts + srv_ip + clt_port + purpose + clt_ip + srv_port
    s += middleproxy_secret + nonce_srv

    if clt_ipv6 and srv_ipv6:
        s += clt_ipv6 + srv_ipv6

    s += nonce_clt

    md5_sum = hashlib.md5(s[1:]).digest()
    sha1_sum = hashlib.sha1(s).digest()

    key = md5_sum[:12] + sha1_sum
    iv = hashlib.md5(s[2:]).digest()
    return key, iv


async def do_middleproxy_handshake(dc_idx):
    START_SEQ_NO = -2
    NONCE_LEN = 16

    RPC_NONCE = b"\xaa\x87\xcb\x7a"
    RPC_HANDSHAKE = b"\xf5\xee\x82\x76"
    CRYPTO_AES = b"\x01\x00\x00\x00"

    RPC_NONCE_ANS_LEN = 32
    RPC_HANDSHAKE_ANS_LEN = 32

    # pass as consts to simplify code
    RPC_FLAGS = b"\x00\x00\x00\x00"
    SENDER_PID = b"IPIPPRPDTIME"
    PEER_PID = b"IPIPPRPDTIME"

    if not 0 <= dc_idx < len(TG_MIDDLE_PROXIES_V4):
        return False
    addr, port = TG_MIDDLE_PROXIES_V4[dc_idx]

    try:
        reader_tgt, writer_tgt = await asyncio.open_connection(addr, port)
    except ConnectionRefusedError as E:
        return False
    except OSError as E:
        return False

    writer_tgt = MTProtoFrameStreamWriter(writer_tgt, START_SEQ_NO)

    key_selector = PROXY_SECRET[:4]
    crypto_ts = int.to_bytes(int(time.time()) % (256**4), 4, "little")

    nonce = bytes([random.randrange(0, 256) for i in range(NONCE_LEN)])

    msg = RPC_NONCE + key_selector + CRYPTO_AES + crypto_ts + nonce

    writer_tgt.write(msg)
    await writer_tgt.drain()

    old_reader = reader_tgt
    reader_tgt = MTProtoFrameStreamReader(reader_tgt, START_SEQ_NO)
    ans = await reader_tgt.read(READ_BUF_SIZE)

    if len(ans) != RPC_NONCE_ANS_LEN:
        return False

    rpc_type, rpc_key_selector, rpc_schema, rpc_crypto_ts, rpc_nonce = (
        ans[:4], ans[4:8], ans[8:12], ans[12:16], ans[16:32]
    )

    if rpc_type != RPC_NONCE or rpc_key_selector != key_selector or rpc_schema != CRYPTO_AES:
        return False

    # get keys
    tg_ip, tg_port = writer_tgt.stream.get_extra_info('peername')
    my_ip, my_port = writer_tgt.stream.get_extra_info('sockname')

    tg_ip_bytes = socket.inet_pton(socket.AF_INET, tg_ip)[::-1]
    my_ip_bytes = socket.inet_pton(socket.AF_INET, global_my_ip)[::-1]

    tg_port_bytes = int.to_bytes(tg_port, 2, "little")
    my_port_bytes = int.to_bytes(my_port, 2, "little")

    # TODO: IPv6 support
    enc_key, enc_iv = get_middleproxy_aes_key_and_iv(
        nonce_srv=rpc_nonce, nonce_clt=nonce, clt_ts=crypto_ts, srv_ip=tg_ip_bytes,
        clt_port=my_port_bytes, purpose=b"CLIENT", clt_ip=my_ip_bytes,
        srv_port=tg_port_bytes, middleproxy_secret=PROXY_SECRET, clt_ipv6=None, srv_ipv6=None)

    dec_key, dec_iv = get_middleproxy_aes_key_and_iv(
        nonce_srv=rpc_nonce, nonce_clt=nonce, clt_ts=crypto_ts, srv_ip=tg_ip_bytes,
        clt_port=my_port_bytes, purpose=b"SERVER", clt_ip=my_ip_bytes,
        srv_port=tg_port_bytes, middleproxy_secret=PROXY_SECRET, clt_ipv6=None, srv_ipv6=None)

    encryptor = create_aes_cbc(key=enc_key, iv=enc_iv)
    decryptor = create_aes_cbc(key=dec_key, iv=dec_iv)

    # TODO: pass client ip and port here for statistics
    handshake = RPC_HANDSHAKE + RPC_FLAGS + SENDER_PID + PEER_PID

    writer_tgt.stream = CryptoWrappedStreamWriter(writer_tgt.stream, encryptor, block_size=16)
    writer_tgt.write(handshake)
    await writer_tgt.drain()

    reader_tgt.stream = CryptoWrappedStreamReader(reader_tgt.stream, decryptor, block_size=16)

    handshake_ans = await reader_tgt.read(1)
    if len(handshake_ans) != RPC_HANDSHAKE_ANS_LEN:
        return False

    handshake_type, handshake_flags, handshake_sender_pid, handshake_peer_pid = (
        handshake_ans[:4], handshake_ans[4:8], handshake_ans[8:20], handshake_ans[20:32])
    if handshake_type != RPC_HANDSHAKE or handshake_peer_pid != SENDER_PID:
        return False

    writer_tgt = ProxyReqStreamWriter(writer_tgt)
    reader_tgt = ProxyReqStreamReader(reader_tgt)

    return reader_tgt, writer_tgt


async def handle_client(reader_clt, writer_clt):
    clt_data = await handle_handshake(reader_clt, writer_clt)
    if not clt_data:
        writer_clt.close()
        return

    reader_clt, writer_clt, user, dc_idx, enc_key_and_iv = clt_data
    
    update_stats(user, connects=1)

    if not USE_MIDDLE_PROXY:
        if FAST_MODE:
            tg_data = await do_direct_handshake(dc_idx, dec_key_and_iv=enc_key_and_iv)
        else:
            tg_data = await do_direct_handshake(dc_idx)
    else:
        tg_data = await do_middleproxy_handshake(dc_idx)

    if not tg_data:
        writer_clt.close()
        return

    reader_tg, writer_tg = tg_data

    if not USE_MIDDLE_PROXY and FAST_MODE:
        class FakeEncryptor:
            def encrypt(self, data):
                return data

        class FakeDecryptor:
            def decrypt(self, data):
                return data

        reader_tg.decryptor = FakeDecryptor()
        writer_clt.encryptor = FakeEncryptor()

    if USE_MIDDLE_PROXY:
        reader_clt = MTProtoCompactFrameStreamReader(reader_clt)
        writer_clt = MTProtoCompactFrameStreamWriter(writer_clt)

    async def connect_reader_to_writer(rd, wr, user):
        update_stats(user, curr_connects_x2=1)
        try:
            while True:
                data = await rd.read(READ_BUF_SIZE)
                if not data:
                    wr.write_eof()
                    await wr.drain()
                    wr.close()
                    return
                else:
                    update_stats(user, octets=len(data))
                    wr.write(data)
                    await wr.drain()
        except (ConnectionResetError, BrokenPipeError, OSError,
                AttributeError, asyncio.streams.IncompleteReadError) as e:
            wr.close()
            # print(e)
        finally:
            update_stats(user, curr_connects_x2=-1)

    asyncio.ensure_future(connect_reader_to_writer(reader_tg, writer_clt, user))
    asyncio.ensure_future(connect_reader_to_writer(reader_clt, writer_tg, user))


async def handle_client_wrapper(reader, writer):
    try:
        await handle_client(reader, writer)
    except (asyncio.IncompleteReadError, ConnectionResetError):
        writer.close()


async def stats_printer():
    global stats
    while True:
        await asyncio.sleep(STATS_PRINT_PERIOD)

        print("Stats for", time.strftime("%d.%m.%Y %H:%M:%S"))
        for user, stat in stats.items():
            print("%s: %d connects (%d current), %.2f MB" % (
                user, stat["connects"], stat["curr_connects_x2"] // 2,
                stat["octets"] / 1000000))
        print(flush=True)


def print_tg_info():
    global USE_MIDDLE_PROXY

    try:
        with urllib.request.urlopen('https://ifconfig.co/ip') as f:
            if f.status != 200:
                raise Exception("Invalid status code")
            my_ip = f.read().decode().strip()
            global global_my_ip
            global_my_ip = my_ip
    except Exception:
        my_ip = 'YOUR_IP'
        if USE_MIDDLE_PROXY:
            print("Failed to determine your ip, advertising disabled", flush=True)
            USE_MIDDLE_PROXY = False

    for user, secret in sorted(USERS.items(), key=lambda x: x[0]):
        params = {
            "server": my_ip, "port": PORT, "secret": secret
        }
        params_encodeded = urllib.parse.urlencode(params, safe=':')
        print("{}: tg://proxy?{}".format(user, params_encodeded), flush=True)


def main():
    init_stats()

    loop = asyncio.get_event_loop()
    stats_printer_task = asyncio.Task(stats_printer())
    asyncio.ensure_future(stats_printer_task)

    task_v4 = asyncio.start_server(handle_client_wrapper,
                                   '0.0.0.0', PORT, loop=loop)
    server_v4 = loop.run_until_complete(task_v4)

    if socket.has_ipv6:
        task_v6 = asyncio.start_server(handle_client_wrapper,
                                       '::', PORT, loop=loop)
        server_v6 = loop.run_until_complete(task_v6)

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass

    stats_printer_task.cancel()

    server_v4.close()
    loop.run_until_complete(server_v4.wait_closed())

    if socket.has_ipv6:
        server_v6.close()
        loop.run_until_complete(server_v6.wait_closed())

    loop.close()


if __name__ == "__main__":
    print_tg_info()
    main()
