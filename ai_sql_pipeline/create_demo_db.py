from __future__ import annotations

import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "runtime_data" / "erp_demo.db"
DEFAULT_SQL_PATH = PROJECT_ROOT / "sql" / "create_erp_demo.sql"


def create_demo_db(
    db_path: str | Path = DEFAULT_DB_PATH,
    sql_path: str | Path = DEFAULT_SQL_PATH,
) -> Path:
    """
    创建用于 Text2SQL 端到端测试的 SQLite 数据库。

    数据库包含：
    - sales_order / purchase_order / supplier / payment：订单与采购域四张表
    """
    db_path = Path(db_path)
    sql_path = Path(sql_path)

    db_path.parent.mkdir(parents=True, exist_ok=True)

    if db_path.exists():
        db_path.unlink()

    sql_text = sql_path.read_text(encoding="utf-8")

    conn = sqlite3.connect(str(db_path))
    conn.executescript(sql_text)
    conn.commit()
    conn.close()

    return db_path


def main() -> None:
    db_path = create_demo_db()
    print(f"已创建测试数据库: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    print("\nsales_order 数据：")
    for row in conn.execute("SELECT * FROM sales_order ORDER BY order_no"):
        print(dict(row))

    print("\nsupplier 数据：")
    for row in conn.execute("SELECT * FROM supplier ORDER BY supplier_id"):
        print(dict(row))

    print("\n验证SQL结果：")
    sql = """
    SELECT supplier.supplier_name, purchase_order.po_amt
    FROM purchase_order
    JOIN supplier
      ON purchase_order.supplier_id = supplier.supplier_id
    WHERE supplier.on_time_rate < 0.9;
    """
    for row in conn.execute(sql):
        print(dict(row))

    conn.close()


if __name__ == "__main__":
    main()
