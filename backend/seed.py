import os
import time

import psycopg2

from rules import weigh


def connect():
    last = None
    for _ in range(30):
        try:
            return psycopg2.connect(os.environ["DATABASE_URL"])
        except psycopg2.OperationalError as exc:
            last = exc
            time.sleep(1)
    raise last


def main():
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS cuppings (
            id serial PRIMARY KEY,
            lot text NOT NULL,
            aroma double precision NOT NULL,
            taste double precision NOT NULL,
            liquor double precision NOT NULL,
            score double precision NOT NULL,
            verdict text NOT NULL,
            note text NOT NULL,
            created_by text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    # 老库补列：自然日归属按 created_at 折算
    cur.execute(
        """ALTER TABLE cuppings
           ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now()"""
    )
    # 审评员签发的某日快照：计数与命中编号集合在签发瞬间冻结
    cur.execute(
        """CREATE TABLE IF NOT EXISTS daily_snapshots (
            id serial PRIMARY KEY,
            day date NOT NULL UNIQUE,
            pass_count integer NOT NULL,
            fail_count integer NOT NULL,
            pass_ids integer[] NOT NULL,
            fail_ids integer[] NOT NULL,
            signed_by text NOT NULL,
            signed_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    cur.execute("SELECT COUNT(*) FROM cuppings")
    if cur.fetchone()[0] == 0:
        for lot, aroma, taste, liquor in (("春茶-A", 8, 8, 7), ("夏茶-C", 5, 4, 6)):
            verdict, note, score = weigh(aroma, taste, liquor)
            cur.execute(
                """INSERT INTO cuppings (lot, aroma, taste, liquor, score, verdict, note, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (lot, aroma, taste, liquor, score, verdict, note, "taster"),
            )
    conn.commit()
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
