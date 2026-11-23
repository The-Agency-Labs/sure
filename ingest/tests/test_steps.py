

def test_camel_case_money_paths_are_blocked():
    from ingest.steps import Allowlist

    al = Allowlist.parse({"navigation": ["^https://bank/"], "post": [r"^https://bank/api/"]})
    for name in ("emtTransfer", "addPayee", "billPayment", "sendMoney", "interacEtransfer"):
        assert al.decide(f"https://bank/api/{name}", "POST", False) == "blocked pattern", name
    for name in ("otvcValidation", "sessions", "accountTransactions", "downloadTransactions"):
        assert al.decide(f"https://bank/api/{name}", "POST", False) == "allow", name


def test_account_ref_keeps_last_four_digits_and_drops_balances():
    from ingest.steps import account_ref

    assert account_ref("No Fee Chequing Account 12345-67890 $15.00") == "no-fee-chequing-account-7890"
    assert account_ref("Cash Back Visa* Card 4111 1111 1111 1234 $0.00") == "cash-back-visa-card-1234"
    assert account_ref("No Fee Chequing Account 12345-67890 $1,015.27") == "no-fee-chequing-account-7890"
    assert account_ref("Visa ending 9911") == "visa-ending-9911"
    assert account_ref("Savings") == "savings"
