import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 所有测试模块共用同一个全新的临时数据库(必须在导入 app 之前设定),
# 避免测试文件收集顺序影响 DATABASE_PATH。
os.environ.setdefault(
    "DATABASE_PATH", os.path.join(tempfile.mkdtemp(prefix="trajtest_"), "test.db")
)
