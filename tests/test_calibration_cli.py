import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pyracast.irradiance.calibration import calibrate
from pyracast.irradiance.pairs import PairedDataset, PairedInterval, WeatherInterval
from scripts.calibrate_irradiance import main

START = datetime(2026, 1, 1, tzinfo=UTC)


def dataset(days: int = 11, start: datetime = START) -> PairedDataset:
    samples = []
    for index in range(days * 48):
        begin = start + timedelta(minutes=30 * index)
        ghi = 100 + (index * 71 % 800)
        temperature = -5 + (index * 37 % 300) / 10
        power = 1000 * (0.01 + 0.85 * ghi / 1000 + 0.02 * (temperature - 25) / 25)
        weather = tuple(
            WeatherInterval(
                begin + timedelta(minutes=5 * part),
                begin + timedelta(minutes=5 * (part + 1)),
                ghi,
                temperature,
                0,
            )
            for part in range(6)
        )
        samples.append(
            PairedInterval(
                begin, begin + timedelta(minutes=30), power, ghi, temperature, 0, weather
            )
        )
    return PairedDataset(tuple(samples), 0, 0, "powerhash", "weatherhash")


def inputs(directory: Path) -> list[str]:
    power, weather = [], []
    for sample in dataset().samples:
        power.append(
            {
                "period_end": sample.end.isoformat(),
                "period": "PT30M",
                "pv_power_rooftop": sample.power_kw,
            }
        )
        weather.extend(
            {
                "period_end": row.end.isoformat(),
                "period": "PT5M",
                "ghi": row.ghi_wm2,
                "air_temp": row.air_temp_c,
                "snow_soiling_rooftop": 0,
            }
            for row in sample.weather
        )
    for name, rows in (("power.json", power), ("weather.json", weather)):
        (directory / name).write_text(json.dumps({"estimated_actuals": rows}), encoding="utf-8")
    return ["--power", "power.json", "--weather", "weather.json", "--timezone", "UTC"]


def test_the_cli_writes_its_outputs_and_summarizes_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    code = main([*inputs(tmp_path), "--capacity-kw", "1000", "--output", "out"])

    assert code == 0
    assert sorted(path.name for path in (tmp_path / "out").iterdir()) == [
        "metrics.json",
        "model.json",
        "predictions.csv",
    ]
    assert "Calibrated" in capsys.readouterr().out


def test_existing_outputs_are_kept_unless_overwrite_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    arguments = [*inputs(tmp_path), "--capacity-kw", "1000", "--output", "out"]
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "model.json").write_text("kept", encoding="utf-8")

    with pytest.raises(SystemExit) as refused:
        main(arguments)

    assert refused.value.code == 2
    assert (tmp_path / "out" / "model.json").read_text(encoding="utf-8") == "kept"
    assert main([*arguments, "--overwrite"]) == 0
    assert json.loads((tmp_path / "out" / "model.json").read_text(encoding="utf-8"))
    assert not [path for path in tmp_path.iterdir() if path.name.startswith(".out")]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--capacity-kw", "0"],
        ["--capacity-kw", "nan"],
        ["--capacity-kw", "-1"],
        ["--capacity-kw", "1000", "--timezone", "Mars/Olympus"],
    ],
)
def test_invalid_capacity_or_timezone_is_refused_before_reading_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as refused:
        main(
            ["--power", "missing.json", "--weather", "missing.json", "--output", "out", *arguments]
        )

    assert refused.value.code == 2
    assert not (tmp_path / "out").exists()


def test_a_timezone_whose_midnight_is_off_the_half_hour_grid_is_named() -> None:
    with pytest.raises(ValueError, match="half-hour"):
        calibrate(dataset(), capacity_kw=1000, timezone="Asia/Kathmandu")
