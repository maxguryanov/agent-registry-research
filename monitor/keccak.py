#!/usr/bin/env python3
"""
keccak256, in pure Python, about sixty lines.

Ethereum event signature hashes are keccak256, which is *not* the SHA-3 in
hashlib: the two differ in one padding byte. Getting the topic hash of an
event requires it, and every library that provides it drags in a build
toolchain, so it lives here instead.

Speed is irrelevant. This hashes a handful of short signature strings once
per process, not user data.

    >>> keccak256(b"Transfer(address,address,uint256)").hex()
    'ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
"""

from __future__ import annotations

_ROUND_CONSTANTS = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]

_ROTATION_OFFSETS = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]

_MASK = (1 << 64) - 1


def _rotl(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (64 - shift))) & _MASK


def _keccak_f(state: list[list[int]]) -> None:
    for rnd in range(24):
        # theta
        c = [state[x][0] ^ state[x][1] ^ state[x][2] ^ state[x][3] ^ state[x][4]
             for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x][y] ^= d[x]

        # rho and pi
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rotl(state[x][y], _ROTATION_OFFSETS[x][y])

        # chi
        for x in range(5):
            for y in range(5):
                state[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y] & _MASK) & b[(x + 2) % 5][y])

        # iota
        state[0][0] ^= _ROUND_CONSTANTS[rnd]


def keccak256(data: bytes) -> bytes:
    rate = 136  # 1088 bits, the rate for keccak256
    state = [[0] * 5 for _ in range(5)]

    # Keccak padding is 0x01 ... 0x80. SHA-3 uses 0x06 here; that one byte is
    # the entire difference between this and hashlib.sha3_256.
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded[-1] |= 0x80

    for offset in range(0, len(padded), rate):
        block = padded[offset:offset + rate]
        for i in range(rate // 8):
            lane = int.from_bytes(block[i * 8:i * 8 + 8], "little")
            state[i % 5][i // 5] ^= lane
        _keccak_f(state)

    out = bytearray()
    while len(out) < 32:
        for i in range(rate // 8):
            if len(out) >= 32:
                break
            out += state[i % 5][i // 5].to_bytes(8, "little")
    return bytes(out[:32])


def event_topic(signature: str) -> str:
    """'Transfer(address,address,uint256)' -> '0xddf252ad...'"""
    return "0x" + keccak256(signature.encode("ascii")).hex()


def function_selector(signature: str) -> str:
    """'tokenURI(uint256)' -> '0xc87b56dd'"""
    return "0x" + keccak256(signature.encode("ascii")).hex()[:8]


if __name__ == "__main__":
    # Known-answer tests. If these pass, the implementation is correct.
    checks = [
        (keccak256(b"").hex(),
         "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"),
        (keccak256(b"abc").hex(),
         "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"),
        (event_topic("Transfer(address,address,uint256)"),
         "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"),
        (function_selector("tokenURI(uint256)"), "0xc87b56dd"),
    ]
    ok = True
    for got, want in checks:
        flag = "ok " if got == want else "FAIL"
        ok &= got == want
        print(f"{flag} {got}")
        if got != want:
            print(f"     expected {want}")
    print("\nall known-answer tests passed" if ok else "\nIMPLEMENTATION IS WRONG")
    raise SystemExit(0 if ok else 1)
