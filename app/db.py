"""SQLite 持久化层。

设计要点:
- 路网按版本注册,版本一旦创建即不可变;修订路网 = 注册新版本。
- 分析结果(匹配路径 + 缺口 + 转向证据)按分析时的路网版本落库,
  因此路网修订后,旧分析仍可按原版本查询。
- 转向证据随分析同事务写入;``turns_computed`` 标记该分析是否带转向证据,
  升级前的旧分析该标记为 0,可查询但不能入选转向调查清单。
- 调查清单按 (路网版本, UTC 日期) 整份替换,与统计查询互不重跑匹配。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "app.db")

# 建立索引前需要确认的列(兼容旧库升级)
INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_analysis_turns_analysis ON analysis_turns(analysis_id);
CREATE INDEX IF NOT EXISTS idx_analysis_turns_lookup ON analysis_turns(network_id, utc_date);
CREATE INDEX IF NOT EXISTS idx_analysis_turns_breakdown
    ON analysis_turns(network_id, utc_date, from_segment_id, to_segment_id, utc_hour);
CREATE INDEX IF NOT EXISTS idx_survey_entries_analysis ON survey_entries(survey_id, analysis_id);
CREATE INDEX IF NOT EXISTS idx_analyses_trajectory ON analyses(trajectory_id);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS networks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version     TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    network_id  INTEGER NOT NULL REFERENCES networks(id),
    segment_id  TEXT NOT NULL,
    start_lat   REAL NOT NULL,
    start_lon   REAL NOT NULL,
    end_lat     REAL NOT NULL,
    end_lon     REAL NOT NULL,
    length_m    REAL NOT NULL,
    UNIQUE (network_id, segment_id)
);

CREATE TABLE IF NOT EXISTS trajectories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trajectory_points (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trajectory_id   INTEGER NOT NULL REFERENCES trajectories(id),
    seq             INTEGER NOT NULL,
    ts              TEXT NOT NULL,   -- UTC ISO8601
    lat             REAL NOT NULL,
    lon             REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS analyses (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    trajectory_id    INTEGER NOT NULL REFERENCES trajectories(id),
    network_id       INTEGER NOT NULL REFERENCES networks(id),
    created_at       TEXT NOT NULL,
    total_points     INTEGER NOT NULL,
    matched_points   INTEGER NOT NULL,
    total_distance_m REAL NOT NULL,
    -- 1 = 本次分析已生成并保存转向证据;0 = 升级前旧分析(无证据,不可入选调查)
    turns_computed   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS analysis_segments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id),
    seq         INTEGER NOT NULL,
    segment_id  TEXT NOT NULL,
    enter_time  TEXT NOT NULL,
    leave_time  TEXT NOT NULL,
    distance_m  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_gaps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id),
    seq         INTEGER NOT NULL,
    start_time  TEXT NOT NULL,
    end_time    TEXT NOT NULL,
    reason      TEXT NOT NULL   -- out_of_range | disconnected
);

-- 转向证据:由同一连续匹配中相邻采样点支撑的转向,一行 = 一个样本转向。
-- from/to_segment_id 取该路网版本下的路段编号;utc_date/utc_hour 取后一个点的时刻。
CREATE TABLE IF NOT EXISTS analysis_turns (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id         INTEGER NOT NULL REFERENCES analyses(id),
    network_id          INTEGER NOT NULL REFERENCES networks(id),
    trajectory_id       INTEGER NOT NULL REFERENCES trajectories(id),
    from_point_index    INTEGER NOT NULL,   -- 前一个采样点在轨迹中的下标
    to_point_index      INTEGER NOT NULL,   -- 后一个采样点在轨迹中的下标
    from_segment_id     TEXT NOT NULL,
    to_segment_id       TEXT NOT NULL,
    from_offset_m       REAL NOT NULL,
    to_offset_m         REAL NOT NULL,
    from_time           TEXT NOT NULL,
    to_time             TEXT NOT NULL,
    gap_seconds         REAL NOT NULL,      -- 两点间隔(秒),0 < gap <= 30
    utc_date            TEXT NOT NULL,      -- YYYY-MM-DD(后一个点的 UTC 日期)
    utc_hour            INTEGER NOT NULL CHECK (utc_hour BETWEEN 0 AND 23)
);

-- 转向调查清单:按 (network_id, utc_date) 分组,整份替换。
CREATE TABLE IF NOT EXISTS surveys (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    network_id     INTEGER NOT NULL REFERENCES networks(id),
    utc_date       TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE (network_id, utc_date)
);

-- 清单条目:同一轨迹在同一清单内最多一条分析。
CREATE TABLE IF NOT EXISTS survey_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    survey_id   INTEGER NOT NULL REFERENCES surveys(id),
    analysis_id INTEGER NOT NULL REFERENCES analyses(id),
    trajectory_id INTEGER NOT NULL REFERENCES trajectories(id),
    UNIQUE (survey_id, trajectory_id)
);
"""


def db_path() -> str:
    return os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH)


def connect() -> sqlite3.Connection:
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        # 旧库升级:补上 analyses.turns_computed 列
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(analyses)")}
        if "turns_computed" not in cols:
            conn.execute(
                "ALTER TABLE analyses ADD COLUMN turns_computed INTEGER NOT NULL DEFAULT 0"
            )
        conn.executescript(INDEX_DDL)
        conn.commit()
    finally:
        conn.close()
