export type Exchange = "NSE" | "BSE";
export type TargetStatus = "Buy zone" | "Sell zone" | "On track" | "Approaching";

export interface Company {
  id: number;
  name: string;
  symbol: string;
  exchange: Exchange;
  price: number;
  change: number;
  buyTarget: number;
  sellTarget: number;
  updated: string;
  sector: string;
}

export interface PortfolioAlert {
  id: string;
  company: string;
  symbol: string;
  type: "Buy target reached" | "Sell target reached" | "Approaching target" | "Stale price" | "Feed interruption";
  price: number;
  target: number;
  time: string;
  status: "New" | "Acknowledged" | "Resolved";
  channel: "Delivered" | "Queued" | "Disabled";
}
