"""每个内置 SQL 模板必须通过 _validate_select_sql(只读 SELECT + 危险词黑名单)。

否则新增/改动模板时,错误(非 SELECT、含 insert/pg_read_file 等)只在运行时对真库才暴露。
"""

import pytest

from ops_agent.sql_templates import SQL_TEMPLATE_METADATA, SQL_TEMPLATES
from ops_agent.tools import _validate_select_sql


@pytest.mark.parametrize("name,sql", sorted(SQL_TEMPLATES.items()), ids=sorted(SQL_TEMPLATES))
def test_template_passes_readonly_validator(name, sql):
    validated, error = _validate_select_sql(sql)
    assert error is None, f"模板 {name} 未通过只读校验:{error}"
    assert validated is not None


def test_template_manifest_matches_template_files():
    assert SQL_TEMPLATES
    assert set(SQL_TEMPLATE_METADATA) == set(SQL_TEMPLATES)
    for name, metadata in SQL_TEMPLATE_METADATA.items():
        assert metadata["risk"] == "read-only", name
        assert metadata["description"], name
        assert metadata["source"], name
