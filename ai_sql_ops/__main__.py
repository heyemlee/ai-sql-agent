"""
业务库只读账号命令行。

    # 打印将要执行的 SQL（不连库，可以先给 DBA 审）
    python -m ai_sql_ops sql --database ai_sql

    # 直接创建（需要有建角色权限的连接串）
    python -m ai_sql_ops provision \
        --admin-dsn postgresql://postgres:***@db:5432/ai_sql \
        --database ai_sql --host db

    # 体检某个连接串是不是真的只读（只读系统目录，生产库也能跑）
    python -m ai_sql_ops audit --dsn "$AI_SQL_BUSINESS_DSN"
"""

from __future__ import annotations

import argparse
import sys

from .readonly_role import ReadonlyRoleSpec, audit_readonly, provision_readonly_role


def _spec_from_args(args) -> ReadonlyRoleSpec:
    return ReadonlyRoleSpec(
        role=args.role,
        database=args.database,
        schema=args.schema,
        password=args.password or "",
        statement_timeout_ms=args.statement_timeout_ms,
    )


def _print_audit(audit) -> bool:
    print(f"账号            : {audit.user}")
    print(f"超级用户        : {'是（危险）' if audit.is_superuser else '否'}")
    print(f"可在 schema 建表: {'是（危险）' if audit.can_create_in_schema else '否'}")
    print(f"有写权限的表    : {len(audit.writable_tables)}")
    print(f"默认只读事务    : {'on' if audit.default_transaction_read_only else 'off'}")
    print(f"statement_timeout: {audit.statement_timeout or '未设置'}")

    problems = audit.problems()
    if problems:
        print("\n发现问题：")
        for item in problems:
            print(f"  ✗ {item}")
        print("\n修复：python -m ai_sql_ops provision --admin-dsn ... 然后改用只读账号的 DSN")
        return False

    print("\n✅ 这个账号是只读的")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="ai_sql_ops", description="AI SQL Agent 业务库只读账号工具"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("sql", "打印建只读角色的 SQL，不连库"),
        ("provision", "在业务库上创建/更新只读角色"),
    ):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("--role", default="ai_sql_ro")
        item.add_argument("--database", default="ai_sql")
        item.add_argument("--schema", default="public")
        item.add_argument("--password", default="", help="留空则随机生成")
        item.add_argument("--statement-timeout-ms", type=int, default=10000)
        if name == "provision":
            item.add_argument("--admin-dsn", required=True, help="有建角色权限的连接串")
            item.add_argument("--host", default="localhost", help="用于拼输出的 DSN")
            item.add_argument("--port", type=int, default=5432)

    audit_parser = sub.add_parser("audit", help="体检某个连接串是不是真的只读")
    audit_parser.add_argument("--dsn", required=True)
    audit_parser.add_argument("--schema", default="public")

    args = parser.parse_args(argv)

    if args.command == "sql":
        print(_spec_from_args(args).to_sql())
        return 0

    if args.command == "provision":
        spec = provision_readonly_role(args.admin_dsn, _spec_from_args(args))
        print("只读角色已就绪。把下面这行配给 AI SQL Agent：\n")
        print(f'  AI_SQL_BUSINESS_DSN="{spec.dsn(args.host, args.port)}"\n')
        print("密码只在这里显示一次，请存进密钥管理。接着体检一下：\n")
        print(f'  python -m ai_sql_ops audit --dsn "{spec.dsn(args.host, args.port)}"')
        return 0

    return 0 if _print_audit(audit_readonly(args.dsn, schema=args.schema)) else 1


if __name__ == "__main__":
    sys.exit(main())
