# signal_dependencies.py

SIGNAL_DEPENDENCIES = {
    "retention_opportunity": [
        "no_email_detected",
        "no_loyalty_program"
    ],
    "conversion_optimization_opportunity": [
        "no_reviews_detected",
        "no_trust_badges",
        "no_upsell_tools"
    ],
    "agency_goldmine": [
        "no_email_detected",
        "no_reviews_detected",
        "no_loyalty_program",
        "no_live_chat"
    ],
    "app_install_target": [
        "no_reviews_detected",
        "no_loyalty_program"
    ],
    "premium_growth_gap": [
        "no_email_detected",
        "no_loyalty_program"
    ],
    "trust_gap": [
        "no_reviews_detected",
        "no_trust_badges",
        "missing_refund_policy"
    ],
    "revenue_leakage": [
        "no_email_detected",
        "no_upsell_tools",
        "no_loyalty_program"
    ],
}
