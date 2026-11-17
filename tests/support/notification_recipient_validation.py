def recipient(length: int) -> str:
    return "a" * (length - 191) + "@" + "b" * 63 + "." + "c" * 63 + "." + "d" * 62
