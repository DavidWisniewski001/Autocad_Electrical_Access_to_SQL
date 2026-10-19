from __future__ import annotations

import dataclasses
import json

import pytest

from acade2sql.schema import ColumnInfo, IndexInfo, TableInfo


def _col(name, ordinal=1, **kw):
    base = dict(
        access_type="VARCHAR", data_type=12, size=10, decimal_digits=None,
        nullable=True, autoincrement=False,
    )
    base.update(kw)
    return ColumnInfo(name=name, ordinal=ordinal, **base)


@pytest.fixture
def table():
    return TableInfo(
        name="T",
        columns=(_col("RECNUM", 1, access_type="COUNTER", autoincrement=True, nullable=False),
                 _col("Cat", 2), _col("DESC", 3)),
        primary_key=("RECNUM",),
        primary_key_source="inferred",
        indexes=(IndexInfo("i1", ("Cat", "DESC"), False),),
    )


def test_frozen(table):
    for obj, attr in ((table, "name"), (table.columns[0], "name"), (table.indexes[0], "unique")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, attr, "x")


def test_column_names(table):
    assert table.column_names == ("RECNUM", "Cat", "DESC")


def test_column_case_insensitive(table):
    assert table.column("cat").name == "Cat"
    assert table.column("CAT").name == "Cat"
    assert table.column("Cat").ordinal == 2


def test_column_missing_raises_keyerror(table):
    with pytest.raises(KeyError):
        table.column("nope")


def test_to_dict_json_serialisable(table):
    d = table.to_dict()
    text = json.dumps(d)
    back = json.loads(text)
    assert back["name"] == "T"
    assert back["primary_key"] == ["RECNUM"]
    assert back["primary_key_source"] == "inferred"
    assert [c["name"] for c in back["columns"]] == ["RECNUM", "Cat", "DESC"]
    assert back["indexes"][0]["columns"] == ["Cat", "DESC"]
    assert back["indexes"][0]["unique"] is False
