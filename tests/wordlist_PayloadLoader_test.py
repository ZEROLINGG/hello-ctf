from pathlib import Path

from ctf import set_debug
from ctf.burp.wordlist import PayloadLoader

set_debug()

ROCKYOU = Path("/home/zz/Documents/passworld/rockyou.txt")


def test_1():
    payload = {}
    with PayloadLoader(
            [("p",ROCKYOU), ("u",ROCKYOU)],
            parallel=True,
        tag="test_1",
        continue_ = True
    ) as loader:
        for _ in range(10):
            payload = loader.next()
            if payload:
                assert payload["u"] == payload["p"]
                
    with PayloadLoader(
        [("p", ROCKYOU), ("u", ROCKYOU)],
        parallel=True,
        tag="test_1",
        continue_=True,
        rollback=1,
    ) as loader:
        assert loader.next() == payload
        for _ in range(10):
            payload = loader.next()
            if payload:
                assert payload["u"] == payload["p"]


def test_2():
    payload = {}
    remaining_count= 0
    with PayloadLoader(
        [("p", ROCKYOU), ("u", ROCKYOU)],
        parallel= False,
        tag="test_2",
        continue_=True,
    ) as loader:
        payload = loader.next()
        for _ in range(10):
            payload_tmp = loader.next()
            assert payload_tmp["p"] == payload["p"]
            assert not payload_tmp["u"] == payload["u"]
            payload = payload_tmp
        remaining_count = loader.remaining_count


    with PayloadLoader(
        [("p", ROCKYOU), ("u", ROCKYOU)],
        parallel= False,
        tag="test_2",
        continue_=True,
        rollback=1,
    ) as loader:
        assert loader.next() == payload
        assert loader.remaining_count == remaining_count
        for _ in range(10):
            payload_tmp = loader.next()
            assert payload_tmp["p"] == payload["p"]
            assert not payload_tmp["u"] == payload["u"]
            payload = payload_tmp
