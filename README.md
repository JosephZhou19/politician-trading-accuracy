# Congressional Trading Disclosure Ingestor

A toolkit for downloading official U.S. Congress stock trade disclosures — from the Senate
and House financial disclosure systems — and storing them in a local, structured database.

No paid third-party API required. All data comes directly from the official public disclosure
sources that Congress members are required to file under the STOCK Act.

## Website

Browse the data at **[josephzhou19.github.io/politician-trading-accuracy](https://josephzhou19.github.io/politician-trading-accuracy/)** —
member and issuer directories, per-trade detail pages with price charts, and activity/position
breakdowns per member. Static site, rebuilt from this data on a schedule; see
`scripts/export_website_data.py` and `.github/workflows/deploy-pages.yml`.

## Disclaimer

This project pulls from publicly available government disclosure systems. It is not
affiliated with, endorsed by, or associated with the U.S. Congress, the Senate, or the House
of Representatives.

## License

See [LICENSE](LICENSE).
