from email.message import EmailMessage
from email.policy import EmailPolicy
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    _Policy = EmailPolicy[EmailMessage]
else:
    _Policy = EmailPolicy


# Folding a long MIME type produces encoded-words, which RFC 2047 section 5 forbids in Content-Type.
class WirePolicy(_Policy):
    def fold_binary(self, name: str, value: Any) -> bytes:
        if name.lower() == "content-type":
            unfolded = EmailPolicy(linesep=self.linesep, cte_type=self.cte_type, max_line_length=0)
            return unfolded.fold_binary(name, value)
        return super().fold_binary(name, value)


def wire_policy(*, eight_bit: bool) -> WirePolicy:
    return WirePolicy(linesep="\r\n", cte_type="8bit" if eight_bit else "7bit")
