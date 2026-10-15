# FunPay Lister

A local browser app for creating and managing FunPay listings. Written in Python and vanilla JavaScript, with no runtime packages required.

**Connecting automatically and permanently deletes paused or zero-stock listings in the selected category.** The app verifies each deletion candidate first and shows progress, results, and a stop button. Active listings with stock are retained. If a listing's eligibility is unclear, it is kept. Read this before entering your account cookie.

## Features

- One listing per item line, with Russian and English title/description templates.
- Every entered price is the **buyer's price in RUB**; the preview also shows what you receive.
- Individual and bulk price changes, editing, pause/reactivate, and deletion.
- A **39-listing cap per category**, including existing offers, checked before each creation.
- Automatic cleanup on connection, with fresh ownership and eligibility checks before deletion.
- Fast connection checks: active listings with known positive stock skip editor requests; uncertain quantities still get checked. Deletions are paced three seconds apart.
- Local draft import/export. Golden Keys remain in memory and are excluded from drafts.

## Run locally

Requires Python **3.10 or newer**. No `pip install` is needed to run the app.

Download and extract the source, then double-click **Start Lister.bat** on Windows, or run:

```sh
python app.py
```

Open **http://127.0.0.1:8765** and keep the app running while using it. To use a different port:

```sh
python app.py --port 8766
```

The server listens only on your computer's loopback interface. It is intended for local use.

## Connect and create listings

1. Sign in to FunPay and copy the `golden_key` cookie value from your browser's developer tools. Treat it like a password; paste it only into the local app, never into an issue or chat.
2. Enter a regular lots-category link, such as `https://funpay.com/lots/3503/`, or just `3503`.
3. Click **Load category**. This starts the automatic permanent cleanup described above. Wait for it to finish, or use **Stop after current listing**.
4. Complete the category options. Add one item name per line and write Russian and English templates. `{item}` inserts the item name; `{price}` inserts the buyer price.
5. Build the batch and set each buyer price in RUB. Check the live price preview and review before publishing.

The whole batch must fit the remaining category slots. For example, 38 existing listings leave space for one new listing. The app does not delete active stocked listings to force the count below the cap.

## Manage existing listings

After connecting, open **My listings → Load my listings**. Edit titles, descriptions, category options, manual stock, and status, or use **Price** to change a buyer price in RUB. Leave the optional price field blank to preserve the current price during a details edit.

Select listings to apply a shared price, pause/reactivate, or delete in bulk. Manual changes have a review screen. Automatic connection cleanup runs separately as described above.

If FunPay does not provide a new-listing editor, the app can connect for management after verifying your seller page. Creating offers stays blocked until a valid editor is available.

## Pricing and request behavior

Buyer prices use FunPay's lowest displayed RUB amount. Other payment methods can cost more. The app uses the live calculator rather than a fixed fee percentage, requires acceptance of inexact targets, and rechecks buyer prices before saving.

Requests that may change listings are never retried automatically. An unconfirmed save/deletion stops the operation; check the listing state on FunPay before trying again. Rate limits, verification pages, and category restrictions are reported rather than bypassed.

New listing creation supports manually fulfilled lots. Automatic-delivery goods and images cannot be uploaded through this app. Existing private goods, images, and purchase messages are preserved during supported edits; an edit is refused if those contents cannot be preserved safely.

## Development and tests

Run the offline Python regression suite:

```sh
python scripts/test.py --python-only
```

With Node.js installed, run Python and JavaScript checks together:

```sh
python scripts/test.py
```

Tests use synthetic credentials and simulated FunPay responses. They do not create, edit, or delete real account listings. Live behavior depends on FunPay's current HTML and account restrictions.

To build a standalone Windows executable, install PyInstaller in a separate virtual environment and run on Windows:

```powershell
python -m PyInstaller --noconfirm --onefile --name "FunPay Lister" --add-data "static;static" app.py
```

## Implementation references

FunPay has no official public listing API used by this project. The form workflow was checked against FunPay's HTML and the community [FunPayCardinal/FunPayAPI implementation](https://github.com/sidor0912/FunPayCardinal/blob/main/FunPayAPI/account.py). That library is not bundled. All account requests go directly from the local app to `https://funpay.com`.

## License

MIT. See [LICENSE](LICENSE).
