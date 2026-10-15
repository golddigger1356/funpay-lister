# Contributing

Use synthetic credentials and offline responses in tests. Do not put Golden Keys, cookies, private delivery goods, screenshots containing account details, or personal drafts in commits or issues.

Run `python scripts/test.py` before proposing a change. Node.js is needed for the JavaScript checks; `--python-only` runs only the Python suite. These checks never contact a real FunPay account.

Keep buyer-RUB pricing, the 39-listing cap, account/category ownership checks, and deletion eligibility checks intact. Changes affecting automatic cleanup should include tests proving active stocked offers are retained and unconfirmed writes are not retried.

Describe the problem, the resulting behavior, and the tests run in your pull request. If a FunPay form changed, provide a minimal sanitized HTML fixture instead of an account response containing secrets.
