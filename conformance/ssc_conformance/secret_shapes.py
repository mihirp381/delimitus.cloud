"""Provider key shapes the bundle scan and the build's gitleaks must agree on (GA-12 F22).

Each shape has a positive (random characters in the provider's format, no live key) that both
scans flag and a placeholder negative that neither does. ``ssc_bundle``'s tests run its scan over
them; ``gates/test_gitleaks_parity.py`` runs gitleaks with the build's config over the same
lines. The values are assembled at run time, so gitleaks on pull requests never sees one whole.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Shape:
    rule: str
    """The ``ssc_bundle.secrets`` rule that flags ``positive``."""
    positive: str
    negative: str


def line(value: str) -> str:
    """A source line that holds ``value`` under a name no name-keyed rule knows."""
    return f'const note = "{value}";\n'


SHAPES: tuple[Shape, ...] = (
    Shape(
        "github_token",
        "ghp_" + "XDVP0unE" + "dP2YFfjc" + "uGnepBfJ" + "oOQHnjdc" + "k99h",
        "ghp_" + "x" * 36,
    ),
    Shape(
        "github_token",
        "github_pat_"
        + "VVrDArXY"
        + "nXRC6TUf"
        + "uZ4Ynf_T"
        + "LLdGnDnD"
        + "9uktKM8i"
        + "f7cxgmRf"
        + "zS2maJUz"
        + "5IOGzNiR"
        + "kzznVPls"
        + "ZEwDAD1C"
        + "pn",
        "github_pat_" + "x" * 82,
    ),
    Shape(
        "gitlab_token",
        "glpat-" + "b2bHmeUX" + "ORIJj3wl" + "dhO6",
        "glpat-" + "x" * 20,
    ),
    Shape(
        "slack_token",
        "xoxb-"
        + "27879240"
        + "5447-783"
        + "67537147"
        + "71-QfyJU"
        + "y9Lybo4C"
        + "5iNaBtFY"
        + "PFy",
        "xoxb-" + "x" * 40,
    ),
    Shape(
        "stripe_live_key",
        "sk_live_" + "3ZcAvdWH" + "9sAGL3EZ" + "3MiemO4d",
        "sk_live_" + "x" * 24,
    ),
    Shape(
        "anthropic_key",
        "sk-ant-api03-"
        + "yBOTAgS3"
        + "nuNmtL4u"
        + "R7SeB81A"
        + "l7JYS7e2"
        + "Rv1Wusni"
        + "ibcDqKTp"
        + "oHdRnwhX"
        + "hRoFAQpY"
        + "GjYZc3wc"
        + "pVxIdVMY"
        + "2FpVMFik"
        + "TMLwvAA",
        "sk-ant-api03-" + "x" * 95,
    ),
    Shape(
        "openai_key",
        "sk-proj-"
        + "qjYZnLOL"
        + "WUN4ymxJ"
        + "WxNU9xSE"
        + "VxYuS_-m"
        + "LDPNyw4N"
        + "riT66GKW"
        + "eqo8H326"
        + "pmUrkTk-"
        + "P_4M4SkM"
        + "u8T3Blbk"
        + "FJ_1Tmvw"
        + "JzIqXs5h"
        + "dTDZRuXA"
        + "SSdF0RIr"
        + "cfDNpNyp"
        + "mPzoy5cu"
        + "1bJkgRiT"
        + "NgYj3hBU"
        + "dFE_3nUs"
        + "dakz",
        "sk-proj-" + "x" * 74 + "T3BlbkFJ" + "x" * 74,
    ),
    Shape(
        "google_api_key",
        "AIza" + "6ofRhFCN" + "lhycqaK_" + "W_h2LWU2" + "1t6y5gK5" + "mCu",
        "AIza" + "x" * 35,
    ),
    Shape(
        "npm_token",
        "npm_" + "0saoc801" + "p1hhvlug" + "vcsvaqux" + "78hncceh" + "03bx",
        "npm_" + "x" * 36,
    ),
    Shape(
        "sendgrid_key",
        "SG."
        + "e8xnidq9"
        + "FBaGABYU"
        + "EswDZW._"
        + "4fT7aaNQ"
        + "fyHyma2d"
        + "Ngem1Yby"
        + "LmRoctsW"
        + "U41nnS1B"
        + "mT",
        "SG." + "x" * 22 + "." + "x" * 43,
    ),
)
