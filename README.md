# Northstar Portfolio Monitor

A responsive React + TypeScript portfolio monitoring demo for a fictional investment management team. All listed companies, prices, market status, alerts, and activity are illustrative; the app does not connect to a market-data provider or backend.

## Run locally

```sh
npm install
npm run dev
```

Create a production build with `npm run build`.

## Demo behavior

- Navigate between the overview, portfolio, alerts, target management, market activity, reports, upload, and settings screens from the sidebar.
- Portfolio prices update on a local simulation interval. The interval and target-zone thresholds can be adjusted in Settings.
- Target edits, alert acknowledgements, theme changes, and imports update local React state only and are not persisted to a server.
- CSV uploads are parsed and validated in the browser. `.xlsx` uploads use sample rows for a clearly labeled simulated preview; workbook contents are not read.
- Reports export a CSV generated from the current mock portfolio.

The project uses Tailwind CSS for utility styling alongside purpose-built responsive styles, Lucide icons, Recharts, and Framer Motion.
