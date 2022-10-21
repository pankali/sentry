from unittest.mock import MagicMock, patch

from sentry.dynamic_sampling.utils import (
    generate_environment_rule,
    generate_rules,
    generate_uniform_rule,
)


def test_generate_uniform_rule_return_rate():
    sample_rate = 0.1
    assert generate_uniform_rule(sample_rate) == {
        "active": True,
        "condition": {"inner": [], "op": "and"},
        "id": 0,
        "sampleRate": sample_rate,
        "type": "trace",
    }


def test_generate_environment_rule():
    bias_env_rule = generate_environment_rule()
    assert bias_env_rule["id"] == 1
    assert bias_env_rule["condition"]["inner"][0] == {
        "op": "glob",
        "name": "trace.environment",
        "value": ["*dev*", "*test*"],
        "options": {"ignoreCase": True},
    }


@patch("sentry.dynamic_sampling.utils.sentry_sdk")
@patch("sentry.dynamic_sampling.utils.quotas.get_blended_sample_rate")
def test_generate_rules_capture_exception(get_blended_sample_rate, sentry_sdk):
    get_blended_sample_rate.return_value = None
    # since we mock get_blended_sample_rate function
    # no need to create real project in DB
    fake_project = MagicMock()
    generate_rules(fake_project)
    get_blended_sample_rate.assert_called_with(fake_project)
    sentry_sdk.capture_exception.assert_called_with()


@patch("sentry.dynamic_sampling.utils.quotas.get_blended_sample_rate")
def test_generate_rules_return_uniform_rules_with_rate(get_blended_sample_rate):
    get_blended_sample_rate.return_value = 0.1
    # since we mock get_blended_sample_rate function
    # no need to create real project in DB
    fake_project = MagicMock()
    assert generate_rules(fake_project) == [
        {
            "active": True,
            "condition": {"inner": [], "op": "and"},
            "id": 0,
            "sampleRate": 0.1,
            "type": "trace",
        }
    ]
    get_blended_sample_rate.assert_called_with(fake_project)


@patch("sentry.dynamic_sampling.utils.quotas.get_blended_sample_rate")
def test_generate_rules_return_uniform_rules_and_env_rule(get_blended_sample_rate):
    get_blended_sample_rate.return_value = 0.1
    # since we mock get_blended_sample_rate function
    # no need to create real project in DB
    fake_project = MagicMock()
    assert generate_rules(fake_project, enable_environment_bias=True) == [
        {
            "sampleRate": 1,
            "type": "trace",
            "condition": {
                "op": "or",
                "inner": [
                    {
                        "op": "glob",
                        "name": "trace.environment",
                        "value": ["*dev*", "*test*"],
                        "options": {"ignoreCase": True},
                    }
                ],
            },
            "active": True,
            "id": 1,
        },
        {
            "active": True,
            "condition": {"inner": [], "op": "and"},
            "id": 0,
            "sampleRate": 0.1,
            "type": "trace",
        },
    ]
    get_blended_sample_rate.assert_called_with(fake_project)


@patch("sentry.dynamic_sampling.utils.quotas.get_blended_sample_rate")
def test_generate_rules_return_uniform_rule_with_100_rate_and_without_env_rule(
    get_blended_sample_rate,
):
    get_blended_sample_rate.return_value = 1.0
    # since we mock get_blended_sample_rate function
    # no need to create real project in DB
    fake_project = MagicMock()
    assert generate_rules(fake_project, enable_environment_bias=True) == [
        {
            "active": True,
            "condition": {"inner": [], "op": "and"},
            "id": 0,
            "sampleRate": 1.0,
            "type": "trace",
        },
    ]
    get_blended_sample_rate.assert_called_with(fake_project)
