"""Constants module — touch every line for coverage."""

from custom_components.finder_you import const


def test_constants_exist():
    assert const.DOMAIN == "finder_you"
    assert const.CONF_EMAIL == "email"
    assert const.CONF_PASSWORD == "password"
    assert const.CONF_PLANT_ID == "plant_id"
    assert const.DEFAULT_SCAN_INTERVAL_SECONDS == 60
    assert const.PLATFORMS == ["cover"]
