"""Docker stats helpers used by Admin Health."""

from __future__ import annotations

from app.services import docker_control


def test_cpu_percent_basic():
    stats = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 2000, "percpu_usage": [1000, 1000]},
            "system_cpu_usage": 10_000,
            "online_cpus": 2,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 1000},
            "system_cpu_usage": 5_000,
        },
    }
    # (1000 / 5000) * 2 * 100 = 40.0
    assert docker_control._cpu_percent(stats) == 40.0


def test_memory_stats_subtracts_cache():
    stats = {
        "memory_stats": {
            "usage": 2000,
            "limit": 8000,
            "stats": {"cache": 500},
        }
    }
    out = docker_control._memory_stats(stats)
    assert out["usageBytes"] == 1500
    assert out["limitBytes"] == 8000
    assert out["percent"] == 18.8


def test_abs_container_aliases():
    svc = docker_control.MANAGED_SERVICES["audiobookshelf"]
    assert svc.container == "audiobookshelf-server"
    assert docker_control._container_candidates(svc) == (
        "audiobookshelf-server",
        "audiobookshelf",
    )


def test_open_url_fallback_port():
    svc = docker_control.MANAGED_SERVICES["prowlarr"]
    url = docker_control._open_url_for_service(svc, info=None)
    assert url == "http://127.0.0.1:9696"
