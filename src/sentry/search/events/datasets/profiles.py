from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Union

from snuba_sdk import OrderBy

from sentry.api.event_search import SearchFilter
from sentry.search.events.constants import PROJECT_ALIAS, PROJECT_NAME_ALIAS
from sentry.search.events.datasets import field_aliases, filter_aliases
from sentry.search.events.datasets.base import DatasetConfig
from sentry.search.events.fields import (
    ColumnArg,
    Combinator,
    Function,
    InvalidFunctionArgument,
    InvalidSearchQuery,
    NullColumn,
    NumberRange,
    NumericColumn,
    SnQLFunction,
    with_default,
)
from sentry.search.events.types import NormalizedArg, ParamsType, SelectType, WhereType


class Kind(Enum):
    DATE = "date"
    DURATION = "duration"
    INTEGER = "integer"
    NUMBER = "number"
    STRING = "string"


class Duration(Enum):
    NANOSECOND = "nanosecond"
    MICROSECOND = "microsecond"
    MILLISECOND = "millisecond"
    SECOND = "second"
    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"


# The only units available right now are duration based
Unit = Duration


@dataclass(frozen=True)
class Column:
    # the external name to expose
    alias: str
    # the internal name in snuba
    column: str
    # type kind/type associated with this column
    kind: Kind
    # some kinds will have an unit associated with it
    unit: Optional[Unit] = None


COLUMNS = [
    Column(alias="organization.id", column="organization_id", kind=Kind.INTEGER),
    Column(alias="project.id", column="project_id", kind=Kind.INTEGER),
    Column(alias="trace.transaction", column="transaction_id", kind=Kind.STRING),
    Column(alias="id", column="profile_id", kind=Kind.STRING),
    Column(alias="timestamp", column="received", kind=Kind.DATE),
    Column(alias="device.arch", column="architecture", kind=Kind.STRING),
    Column(alias="device.classification", column="device_classification", kind=Kind.STRING),
    Column(alias="device.locale", column="device_locale", kind=Kind.STRING),
    Column(alias="device.manufacturer", column="device_manufacturer", kind=Kind.STRING),
    Column(alias="device.model", column="device_model", kind=Kind.STRING),
    Column(alias="os.build", column="device_os_build_number", kind=Kind.STRING),
    Column(alias="os.name", column="device_os_name", kind=Kind.STRING),
    Column(alias="os.version", column="device_os_version", kind=Kind.STRING),
    Column(
        alias="profile.duration", column="duration_ns", kind=Kind.DURATION, unit=Duration.NANOSECOND
    ),
    Column(alias="environment", column="environment", kind=Kind.STRING),
    Column(alias="platform.name", column="platform", kind=Kind.STRING),
    Column(alias="trace", column="trace_id", kind=Kind.STRING),
    Column(alias="transaction", column="transaction_name", kind=Kind.STRING),
    # There is a `version_code` column that exists for
    # legacy profiles, we've decided not to support that.
    Column(alias="release", column="version_name", kind=Kind.STRING),
    # We want to alias `project_id` to the column as well
    # because the query builder uses that internally
    Column(alias="project_id", column="project_id", kind=Kind.INTEGER),
]

COLUMN_MAP = {column.alias: column for column in COLUMNS}


class ProfileColumnArg(ColumnArg):
    def normalize(
        self, value: str, params: ParamsType, combinator: Optional[Combinator]
    ) -> NormalizedArg:
        column = COLUMN_MAP.get(value)

        if column is None:
            raise InvalidFunctionArgument(f"{value} is not a valid column")

        return value


class ProfileNumericColumn(NumericColumn):
    def _normalize(self, value: str) -> str:
        column = COLUMN_MAP.get(value)

        if column is None:
            raise InvalidFunctionArgument(f"{value} is not a valid column")

        if (
            column.kind == Kind.INTEGER
            or column.kind == Kind.DURATION
            or column.kind == Kind.NUMBER
        ):
            return column.column

        raise InvalidFunctionArgument(f"{value} is not a numeric column")

    def get_type(self, value: str) -> str:
        try:
            return COLUMN_MAP[value].kind.value
        except KeyError:
            return Kind.NUMBER.value


class ProfilesDatasetConfig(DatasetConfig):
    def __init__(self, builder: Any):
        self.builder = builder

    @property
    def search_filter_converter(
        self,
    ) -> Mapping[str, Callable[[SearchFilter], Optional[WhereType]]]:
        return {
            PROJECT_ALIAS: self._project_slug_filter_converter,
            PROJECT_NAME_ALIAS: self._project_slug_filter_converter,
        }

    def _project_slug_filter_converter(self, search_filter: SearchFilter) -> Optional[WhereType]:
        return filter_aliases.project_slug_converter(self.builder, search_filter)

    @property
    def field_alias_converter(self) -> Mapping[str, Callable[[str], SelectType]]:
        return {
            PROJECT_ALIAS: self._resolve_project_slug_alias,
            PROJECT_NAME_ALIAS: self._resolve_project_slug_alias,
        }

    def _resolve_project_slug_alias(self, alias: str) -> SelectType:
        return field_aliases.resolve_project_slug_alias(self.builder, alias)

    @property
    def function_converter(self) -> Mapping[str, SnQLFunction]:
        return {
            function.name: function
            for function in [
                # TODO: A lot of this is duplicated from the discover dataset.
                # Ideally, we refactor it to be shared across datasets.
                SnQLFunction(
                    "last_seen",
                    snql_aggregate=lambda _, alias: Function(
                        "max",
                        [self.builder.column("timestamp")],
                        alias,
                    ),
                    default_result_type="date",
                    redundant_grouping=True,
                ),
                SnQLFunction(
                    "latest_event",
                    snql_aggregate=lambda _, alias: Function(
                        "argMax",
                        [self.builder.column("id"), self.builder.column("timestamp")],
                        alias,
                    ),
                    default_result_type="string",
                ),
                SnQLFunction(
                    "count",
                    optional_args=[NullColumn("column")],
                    snql_aggregate=lambda _, alias: Function(
                        "count",
                        [],
                        alias,
                    ),
                    default_result_type="integer",
                ),
                SnQLFunction(
                    "count_unique",
                    required_args=[ProfileColumnArg("column")],
                    snql_aggregate=lambda args, alias: Function("uniq", [args["column"]], alias),
                    default_result_type="integer",
                ),
                SnQLFunction(
                    "percentile",
                    required_args=[
                        ProfileNumericColumn("column"),
                        NumberRange("percentile", 0, 1),
                    ],
                    snql_aggregate=self._resolve_percentile,
                    result_type_fn=self.reflective_result_type(),
                    default_result_type="duration",
                    redundant_grouping=True,
                ),
                SnQLFunction(
                    "p50",
                    optional_args=[
                        with_default("profile.duration", ProfileNumericColumn("column")),
                    ],
                    snql_aggregate=lambda args, alias: self._resolve_percentile(args, alias, 0.5),
                    result_type_fn=self.reflective_result_type(),
                    default_result_type="duration",
                    redundant_grouping=True,
                ),
                SnQLFunction(
                    "p75",
                    optional_args=[
                        with_default("profile.duration", ProfileNumericColumn("column")),
                    ],
                    snql_aggregate=lambda args, alias: self._resolve_percentile(args, alias, 0.75),
                    result_type_fn=self.reflective_result_type(),
                    default_result_type="duration",
                    redundant_grouping=True,
                ),
                SnQLFunction(
                    "p95",
                    optional_args=[
                        with_default("profile.duration", ProfileNumericColumn("column")),
                    ],
                    snql_aggregate=lambda args, alias: self._resolve_percentile(args, alias, 0.95),
                    result_type_fn=self.reflective_result_type(),
                    default_result_type="duration",
                    redundant_grouping=True,
                ),
                SnQLFunction(
                    "p99",
                    optional_args=[
                        with_default("profile.duration", ProfileNumericColumn("column")),
                    ],
                    snql_aggregate=lambda args, alias: self._resolve_percentile(args, alias, 0.95),
                    result_type_fn=self.reflective_result_type(),
                    default_result_type="duration",
                    redundant_grouping=True,
                ),
                SnQLFunction(
                    "p100",
                    optional_args=[
                        with_default("profile.duration", ProfileNumericColumn("column")),
                    ],
                    snql_aggregate=lambda args, alias: self._resolve_percentile(args, alias, 1),
                    result_type_fn=self.reflective_result_type(),
                    default_result_type="duration",
                    redundant_grouping=True,
                ),
                SnQLFunction(
                    "min",
                    required_args=[ProfileNumericColumn("column")],
                    snql_aggregate=lambda args, alias: Function("min", [args["column"]], alias),
                    result_type_fn=self.reflective_result_type(),
                    default_result_type="duration",
                    redundant_grouping=True,
                ),
                SnQLFunction(
                    "max",
                    required_args=[ProfileNumericColumn("column")],
                    snql_aggregate=lambda args, alias: Function("max", [args["column"]], alias),
                    result_type_fn=self.reflective_result_type(),
                    default_result_type="duration",
                    redundant_grouping=True,
                ),
                SnQLFunction(
                    "avg",
                    required_args=[ProfileNumericColumn("column")],
                    snql_aggregate=lambda args, alias: Function("avg", [args["column"]], alias),
                    result_type_fn=self.reflective_result_type(),
                    default_result_type="duration",
                    redundant_grouping=True,
                ),
                SnQLFunction(
                    "sum",
                    required_args=[ProfileNumericColumn("column")],
                    snql_aggregate=lambda args, alias: Function("sum", [args["column"]], alias),
                    result_type_fn=self.reflective_result_type(),
                    default_result_type="duration",
                ),
            ]
        }

    @property
    def orderby_converter(self) -> Mapping[str, OrderBy]:
        return {}

    def resolve_column(self, column: str) -> str:
        try:
            return COLUMN_MAP[column].column
        except KeyError:
            raise InvalidSearchQuery(f"Unknown field: {column}")

    def resolve_column_type(self, column: str) -> Optional[str]:
        try:
            col = COLUMN_MAP[column]
            if col.unit:
                # if the column has an associated unit, prioritize that
                return col.unit.value
            return col.kind.value
        except KeyError:
            return None

    def _resolve_percentile(
        self,
        args: Mapping[str, Union[str, Column, SelectType, int, float]],
        alias: str,
        fixed_percentile: Optional[float] = None,
    ) -> SelectType:
        return (
            Function(
                "max",
                [args["column"]],
                alias,
            )
            if fixed_percentile == 1
            else Function(
                f'quantile({fixed_percentile if fixed_percentile is not None else args["percentile"]})',
                [args["column"]],
                alias,
            )
        )
