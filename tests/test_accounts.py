"""自助登录单测（无 DB 部分：密码哈希、用户名校验、限速窗口）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import os

os.environ.setdefault("DASHSCOPE_API_KEY", "sk-dummy-for-test")

from self_agent.gateway import accounts  # noqa: E402


def test_password_hash_salted():
    salt1, salt2 = b"a" * 16, b"b" * 16
    h1 = accounts._hash("password1", salt1)
    assert h1 == accounts._hash("password1", salt1)      # 确定性
    assert h1 != accounts._hash("password1", salt2)      # 盐生效
    assert h1 != accounts._hash("password2", salt1)      # 密码生效
    assert len(h1) == 128                                 # scrypt 64B hex


def test_username_validation():
    assert accounts._valid_username("zhang_san-01")
    assert accounts._valid_username("张三")
    assert not accounts._valid_username("a")              # 太短
    assert not accounts._valid_username("bad name")       # 空格
    assert not accounts._valid_username("x" * 33)         # 太长


def test_login_rate_limit_window():
    import time

    accounts._fail_counts["tuser"] = [time.time()] * 5
    try:
        accounts.login("tuser", "x")
        raise AssertionError("应触发限速")
    except ValueError as e:
        assert "失败次数过多" in str(e)
    finally:
        accounts._fail_counts.pop("tuser", None)
