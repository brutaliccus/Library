"""Listener UX API smoke checks (play queue column + history route shape)."""

from app.models import User


def test_user_has_play_queue_json_column():
    assert hasattr(User, "play_queue_json")
    col = User.__table__.c.play_queue_json
    assert col.nullable is True


def test_stream_history_route_registered():
    from app.routers import stream

    paths = {getattr(r, "path", "") for r in stream.router.routes}
    assert "/history" in paths or any(p.endswith("/history") for p in paths)
    assert any("play-queue" in (getattr(r, "path", "") or "") for r in stream.router.routes)


def test_personalized_shelves_route_registered():
    from app.routers import books

    assert any(
        "personalized-shelves" in (getattr(r, "path", "") or "")
        for r in books.router.routes
    )
