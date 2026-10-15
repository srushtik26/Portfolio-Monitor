import type { Company, PortfolioAlert } from "./types";

const raw: Array<[string, string, string, number, number, number, number, string]> = [
  ["Reliance Industries", "RELIANCE", "NSE", 2942.4, 1.24, 2780, 3180, "Energy"],
  ["HDFC Bank", "HDFCBANK", "NSE", 1684.7, -0.42, 1575, 1820, "Banking"],
  ["Tata Consultancy Services", "TCS", "NSE", 4128.25, 0.86, 4140, 4380, "Technology"],
  ["ICICI Bank", "ICICIBANK", "NSE", 1248.9, 1.12, 1160, 1340, "Banking"],
  ["Infosys", "INFY", "NSE", 1832.6, -1.18, 1720, 1980, "Technology"],
  ["Bharti Airtel", "BHARTIARTL", "NSE", 1562.3, 0.64, 1460, 1680, "Telecom"],
  ["ITC", "ITC", "NSE", 438.75, -0.28, 410, 475, "FMCG"],
  ["Larsen & Toubro", "LT", "NSE", 3624.1, 1.86, 3380, 3860, "Industrials"],
  ["State Bank of India", "SBIN", "NSE", 812.55, 0.32, 760, 880, "Banking"],
  ["Hindustan Unilever", "HINDUNILVR", "NSE", 2386.4, -0.76, 2250, 2580, "FMCG"],
  ["Axis Bank", "AXISBANK", "NSE", 1184.2, 0.54, 1100, 1275, "Banking"],
  ["Wipro", "WIPRO", "NSE", 524.65, -0.92, 490, 575, "Technology"],
  ["Tata Motors", "TATAMOTORS", "NSE", 986.3, 2.34, 910, 1050, "Automotive"],
  ["Maruti Suzuki", "MARUTI", "NSE", 12428, 0.78, 11700, 13200, "Automotive"],
  ["Asian Paints", "ASIANPAINT", "NSE", 2284.75, -1.46, 2150, 2490, "Consumer"],
  ["Bajaj Finance", "BAJFINANCE", "NSE", 7248.5, 1.08, 6800, 7750, "Financials"],
  ["Sun Pharmaceutical", "SUNPHARMA", "NSE", 1748.2, 0.43, 1640, 1880, "Healthcare"],
  ["Titan Company", "TITAN", "NSE", 3426.8, -0.34, 3220, 3690, "Consumer"],
  ["Adani Ports", "ADANIPORTS", "NSE", 1388.6, 1.62, 1290, 1490, "Infrastructure"],
  ["Kotak Mahindra Bank", "KOTAKBANK", "NSE", 1784.35, -0.65, 1680, 1925, "Banking"],
  ["NTPC", "NTPC", "NSE", 362.15, 0.94, 338, 392, "Utilities"],
  ["Power Grid Corporation", "POWERGRID", "NSE", 328.45, -0.18, 306, 358, "Utilities"],
  ["UltraTech Cement", "ULTRACEMCO", "NSE", 11248, 0.72, 10600, 12000, "Materials"],
  ["Nestle India", "NESTLEIND", "NSE", 2468.3, -0.58, 2310, 2680, "FMCG"],
  ["HCL Technologies", "HCLTECH", "NSE", 1892.7, 1.34, 1770, 2040, "Technology"],
  ["Tech Mahindra", "TECHM", "NSE", 1682.5, 0.28, 1570, 1820, "Technology"],
  ["Mahindra & Mahindra", "M&M", "NSE", 2784.2, 1.48, 2600, 2990, "Automotive"],
  ["Tata Steel", "TATASTEEL", "NSE", 154.85, -0.86, 145, 168, "Materials"],
  ["JSW Steel", "JSWSTEEL", "NSE", 924.6, 0.52, 865, 998, "Materials"],
  ["Coal India", "COALINDIA", "NSE", 462.3, -0.24, 430, 498, "Energy"],
  ["Dr. Reddy's Laboratories", "DRREDDY", "NSE", 1284.8, 0.36, 1200, 1385, "Healthcare"],
  ["Cipla", "CIPLA", "NSE", 1468.5, 0.82, 1370, 1580, "Healthcare"],
  ["Bajaj Auto", "BAJAJ-AUTO", "NSE", 9642, -0.42, 9050, 10350, "Automotive"],
  ["Eicher Motors", "EICHERMOT", "NSE", 4862.4, 1.16, 4550, 5210, "Automotive"],
  ["IndusInd Bank", "INDUSINDBK", "NSE", 1028.65, -1.72, 965, 1110, "Banking"],
  ["Apollo Hospitals", "APOLLOHOSP", "NSE", 6842, 0.68, 6420, 7330, "Healthcare"],
  ["Trent", "TRENT", "BSE", 6828.5, 2.82, 6400, 6750, "Retail"],
  ["Zomato", "ZOMATO", "NSE", 248.6, 3.14, 232, 245, "Technology"],
  ["Bharat Electronics", "BEL", "NSE", 312.45, 1.92, 292, 335, "Defence"],
  ["SBI Life Insurance", "SBILIFE", "BSE", 1684.2, -0.38, 1580, 1815, "Insurance"],
];

export const initialCompanies: Company[] = raw.map((r, i) => ({
  id: i + 1, name: r[0], symbol: r[1], exchange: r[2] as Company["exchange"],
  price: Number(r[3]), change: Number(r[4]), buyTarget: Number(r[5]), sellTarget: Number(r[6]),
  updated: i === 14 ? "09:14:08" : i === 34 ? "09:12:46" : "09:28:32", sector: r[7],
}));

export const initialAlerts: PortfolioAlert[] = [
  { id: "a1", company: "Zomato", symbol: "ZOMATO", type: "Sell target reached", price: 248.6, target: 245, time: "2 min ago", status: "New", channel: "Delivered" },
  { id: "a2", company: "Tata Motors", symbol: "TATAMOTORS", type: "Approaching target", price: 986.3, target: 1050, time: "18 min ago", status: "New", channel: "Delivered" },
  { id: "a3", company: "Asian Paints", symbol: "ASIANPAINT", type: "Approaching target", price: 2284.75, target: 2150, time: "34 min ago", status: "Acknowledged", channel: "Delivered" },
  { id: "a4", company: "Infosys", symbol: "INFY", type: "Approaching target", price: 1832.6, target: 1720, time: "1 hr ago", status: "Acknowledged", channel: "Queued" },
  { id: "a5", company: "Trent", symbol: "TRENT", type: "Sell target reached", price: 6828.5, target: 6750, time: "1 hr ago", status: "New", channel: "Delivered" },
  { id: "a6", company: "IndusInd Bank", symbol: "INDUSINDBK", type: "Stale price", price: 1028.65, target: 965, time: "2 hr ago", status: "New", channel: "Queued" },
  { id: "a7", company: "TCS", symbol: "TCS", type: "Buy target reached", price: 4128.25, target: 4140, time: "Yesterday", status: "Resolved", channel: "Delivered" },
];

export function getStatus(company: Company, buyThreshold = 2, sellThreshold = 2): "Buy zone" | "Sell zone" | "On track" | "Approaching" {
  const buyGap = ((company.price - company.buyTarget) / company.buyTarget) * 100;
  const sellGap = ((company.sellTarget - company.price) / company.sellTarget) * 100;
  if (buyGap <= buyThreshold) return "Buy zone";
  if (sellGap <= sellThreshold) return "Sell zone";
  if (buyGap < 7 || sellGap < 7) return "Approaching";
  return "On track";
}

export function historyFor(company: Company, period = "1M") {
  const points = period === "1D" ? 20 : period === "1W" ? 28 : period === "3M" ? 40 : period === "1Y" ? 48 : 36;
  return Array.from({ length: points }, (_, i) => {
    const wave = Math.sin(i * 0.58 + company.id) * 0.012 + Math.cos(i * 0.24 + company.id) * 0.008;
    const drift = (i / points - 0.48) * (company.change / 100);
    const label = period === "1D"
      ? `${String(9 + Math.floor((i * 6) / points)).padStart(2, "0")}:${String((i * 10) % 60).padStart(2, "0")}`
      : period === "1W"
        ? ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][i % 7]
        : `${["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][i % 12]} ${4 + i}`;
    return { label, price: Math.round(company.price * (1 + wave + drift) * 100) / 100 };
  });
}
