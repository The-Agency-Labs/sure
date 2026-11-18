

def test_camel_case_money_paths_are_blocked():
    from ingest.steps import Allowlist

    al = Allowlist.parse({"navigation": ["^https://bank/"], "post": [r"^https://bank/api/"]})
    for name in ("emtTransfer", "addPayee", "billPayment", "sendMoney", "interacEtransfer"):
        assert al.decide(f"https://bank/api/{name}", "POST", False) == "blocked pattern", name
    for name in ("otvcValidation", "sessions", "accountTransactions", "downloadTransactions"):
        assert al.decide(f"https://bank/api/{name}", "POST", False) == "allow", name
