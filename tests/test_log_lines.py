from pyracast.log_lines import CONTINUATION, LINE_CHARACTERS, one_record


def test_every_line_after_the_first_of_a_record_is_indented() -> None:
    assert one_record("start\r\nsecond\rthird\n") == (
        f"start\n{CONTINUATION}second\n{CONTINUATION}third\n{CONTINUATION}"
    )


def test_a_long_line_continues_on_indented_lines_of_bounded_length() -> None:
    text = "x" * (2 * LINE_CHARACTERS + 1)

    lines = one_record(text).split("\n")

    assert [len(line) for line in lines] == [
        LINE_CHARACTERS,
        len(CONTINUATION) + LINE_CHARACTERS,
        len(CONTINUATION) + 1,
    ]
    assert "".join(line.removeprefix(CONTINUATION) for line in lines) == text
