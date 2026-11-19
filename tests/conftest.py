"""Hypothesis profiles. gate: the property counts the gate requires.
red: few examples, for the mutation-red runs (tests/red/red_checks.py) and mutmut. dev: a quick local run."""
import os

from hypothesis import HealthCheck, settings

settings.register_profile('gate', max_examples=3000, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
settings.register_profile('red', max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
settings.register_profile('dev', max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
settings.load_profile(os.environ.get('HYPOTHESIS_PROFILE', 'gate'))
