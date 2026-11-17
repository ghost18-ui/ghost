//+------------------------------------------------------------------+
//| AuctionSignalEA.mq5                                              |
//| Polls your Auction Scanner server and places the trades it       |
//| signals. Starts in DRY-RUN mode (logs only) and refuses to run   |
//| on a live account unless AllowLive = true.                       |
//|                                                                  |
//| NOT COMPILED OR TESTED by its author (no MetaEditor available   |
//| when it was written). Compile it, run it in DryRun on a demo     |
//| account first, and read the Experts log before trusting it.      |
//+------------------------------------------------------------------+
#property copyright "Auction Scanner"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>

input group "Connection"
input string ServerUrl       = "https://YOUR-APP.onrender.com";  // no trailing slash
input string AccessToken     = "";                               // same as ACCESS_TOKEN on the server
input int    PollSeconds     = 5;
input int    MaxSignalAgeSec = 120;     // ignore signals older than this
input string SymbolMap       = "SPY=SPY,QQQ=QQQ,GLD=GLD,USO=USO,EUR/USD=EURUSD,XAU/USD=XAUUSD,WTI=USOIL,US100=USTEC,US30=US30";

input group "Safety"
input bool   DryRun          = true;    // true = log what it WOULD do, place nothing
input bool   AllowLive       = false;   // must be true to run on a real-money account
input int    MaxTradesPerDay = 3;
input double MaxDailyLossPct = 2.0;     // stop opening trades after this % of balance lost today
input int    MaxSpreadPoints = 0;       // 0 = no spread filter

input group "Trade"
input double RiskPercent     = 0.5;     // % of balance risked per trade (to the stop)
input double PartialPercent  = 50.0;    // % closed at TP1 (0 = no partial)
input bool   MoveToBreakEven = true;    // after TP1 is reached
input ulong  MagicNumber     = 760011;
input int    DeviationPoints = 20;

CTrade   trade;
long     g_lastId   = -1;
string   g_gvLast;
datetime g_lastHeartbeatLog = 0;

//------------------------------------------------------------------
string MapSymbol(const string src)
{
   string pairs[];
   int n = StringSplit(SymbolMap, ',', pairs);
   for(int i = 0; i < n; i++)
   {
      string kv[];
      if(StringSplit(pairs[i], '=', kv) == 2)
      {
         string a = kv[0], b = kv[1];
         StringTrimLeft(a); StringTrimRight(a); StringTrimLeft(b); StringTrimRight(b);
         if(a == src) return b;
      }
   }
   return "";
}

//------------------------------------------------------------------
bool Http(const string path, string &body)
{
   string url = ServerUrl + path;
   char post[], result[];
   ArrayResize(post, 0);
   string headers = "X-Token: " + AccessToken + "\r\n";
   string rh;
   ResetLastError();
   int code = WebRequest("GET", url, headers, 8000, post, result, rh);
   if(code == -1)
   {
      int e = GetLastError();
      if(e == 4014)
         Print("WebRequest blocked. Add ", ServerUrl, " under Tools > Options > Expert Advisors > Allow WebRequest for listed URL.");
      else
         Print("WebRequest failed, error ", e);
      return false;
   }
   if(code == 401) { Print("Server rejected the access token (401). Check AccessToken."); return false; }
   if(code != 200) { Print("Server returned HTTP ", code); return false; }
   body = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   return true;
}

//------------------------------------------------------------------
string PollPath(long after)
{
   string mode = (AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_REAL) ? "real" : "demo";
   return "/api/ea/poll?after=" + (string)after +
          "&acct=" + (string)AccountInfoInteger(ACCOUNT_LOGIN) +
          "&mode=" + mode + "&dry=" + (DryRun ? "1" : "0");
}

//------------------------------------------------------------------
int OnInit()
{
   ENUM_ACCOUNT_TRADE_MODE m = (ENUM_ACCOUNT_TRADE_MODE)AccountInfoInteger(ACCOUNT_TRADE_MODE);
   if(m == ACCOUNT_TRADE_MODE_REAL && !AllowLive)
   {
      Alert("AuctionSignalEA: this is a REAL account and AllowLive is false. EA stopped.");
      return INIT_FAILED;
   }
   if(StringLen(AccessToken) == 0) { Alert("AuctionSignalEA: set AccessToken."); return INIT_FAILED; }
   if(RiskPercent <= 0 || RiskPercent > 5) { Alert("AuctionSignalEA: RiskPercent must be above 0 and at most 5."); return INIT_FAILED; }

   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(DeviationPoints);

   g_gvLast = "ASEA_last_" + (string)AccountInfoInteger(ACCOUNT_LOGIN);
   if(GlobalVariableCheck(g_gvLast))
      g_lastId = (long)GlobalVariableGet(g_gvLast);
   else
   {
      // First run: skip everything already on the server so old signals are never traded.
      string body;
      if(!Http(PollPath(-1), body)) { Print("Could not reach the server at start-up. Will retry."); }
      else
      {
         string lines[];
         if(StringSplit(body, '\n', lines) > 0)
         {
            string head[];
            if(StringSplit(lines[0], '|', head) >= 2 && head[0] == "OK")
            {
               g_lastId = (long)StringToInteger(head[1]);
               GlobalVariableSet(g_gvLast, (double)g_lastId);
            }
         }
      }
   }
   EventSetTimer(MathMax(1, PollSeconds));
   Print("AuctionSignalEA started. DryRun=", DryRun, " last signal id=", g_lastId,
         " account mode=", EnumToString(m));
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) { EventKillTimer(); }

//------------------------------------------------------------------
void OnTimer()
{
   ManagePositions();

   string body;
   if(g_lastId < 0)   // start-up call failed earlier
   {
      if(!Http(PollPath(-1), body)) return;
      string l0[]; StringSplit(body, '\n', l0);
      string h0[];
      if(ArraySize(l0) > 0 && StringSplit(l0[0], '|', h0) >= 2 && h0[0] == "OK")
      { g_lastId = (long)StringToInteger(h0[1]); GlobalVariableSet(g_gvLast, (double)g_lastId); }
      return;
   }

   if(!Http(PollPath(g_lastId), body)) return;

   string lines[];
   int n = StringSplit(body, '\n', lines);
   if(n < 1) return;
   string head[];
   if(StringSplit(lines[0], '|', head) < 2 || head[0] != "OK") { Print("Unexpected server reply: ", lines[0]); return; }

   for(int i = 1; i < n; i++)
   {
      StringTrimRight(lines[i]);
      if(StringLen(lines[i]) == 0) continue;
      string f[];
      if(StringSplit(lines[i], '|', f) < 9) { Print("Bad signal line: ", lines[i]); continue; }
      long id = (long)StringToInteger(f[0]);
      if(id <= g_lastId) continue;
      g_lastId = id;                                   // mark as seen BEFORE acting: never trade one signal twice
      GlobalVariableSet(g_gvLast, (double)g_lastId);
      HandleSignal(id, f[1], f[2], StringToDouble(f[3]), StringToDouble(f[4]), StringToDouble(f[5]),
                   (int)StringToInteger(f[6]), f[7], f[8]);
   }
}

//------------------------------------------------------------------
datetime DayStart() { MqlDateTime t; TimeCurrent(t); t.hour = 0; t.min = 0; t.sec = 0; return StructToTime(t); }

int TradesToday()
{
   int c = 0;
   if(!HistorySelect(DayStart(), TimeCurrent() + 60)) return 0;
   for(int i = 0; i < HistoryDealsTotal(); i++)
   {
      ulong tk = HistoryDealGetTicket(i);
      if(HistoryDealGetInteger(tk, DEAL_MAGIC) == (long)MagicNumber && HistoryDealGetInteger(tk, DEAL_ENTRY) == DEAL_ENTRY_IN) c++;
   }
   return c;
}

double PnlToday()
{
   double p = 0;
   if(HistorySelect(DayStart(), TimeCurrent() + 60))
      for(int i = 0; i < HistoryDealsTotal(); i++)
      {
         ulong tk = HistoryDealGetTicket(i);
         if(HistoryDealGetInteger(tk, DEAL_MAGIC) != (long)MagicNumber) continue;
         if(HistoryDealGetInteger(tk, DEAL_ENTRY) == DEAL_ENTRY_IN) continue;
         p += HistoryDealGetDouble(tk, DEAL_PROFIT) + HistoryDealGetDouble(tk, DEAL_SWAP) + HistoryDealGetDouble(tk, DEAL_COMMISSION);
      }
   for(int j = 0; j < PositionsTotal(); j++)
   {
      ulong tk = PositionGetTicket(j);
      if(tk == 0 || PositionGetInteger(POSITION_MAGIC) != (long)MagicNumber) continue;
      p += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
   }
   return p;
}

bool HaveOpenPosition(const string sym)
{
   for(int j = 0; j < PositionsTotal(); j++)
   {
      ulong tk = PositionGetTicket(j);
      if(tk == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) == (long)MagicNumber && PositionGetString(POSITION_SYMBOL) == sym) return true;
   }
   return false;
}

//------------------------------------------------------------------
double LotsForRisk(const string sym, double slDist)
{
   double tickSize  = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   double tickValue = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE_LOSS);
   if(tickValue <= 0) tickValue = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
   if(tickSize <= 0 || tickValue <= 0 || slDist <= 0) return 0;
   double riskMoney   = AccountInfoDouble(ACCOUNT_BALANCE) * RiskPercent / 100.0;
   double lossPerLot  = slDist / tickSize * tickValue;
   double lots        = riskMoney / lossPerLot;
   double step = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   double vmin = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN), vmax = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   if(step <= 0) return 0;
   lots = MathFloor(lots / step) * step;          // round DOWN so risk never exceeds the setting
   if(lots < vmin) return 0;                       // minimum lot would risk more than allowed: skip
   return MathMin(lots, vmax);
}

//------------------------------------------------------------------
void HandleSignal(long id, string srcSym, string side, double slD, double tp1D, double tp2D, int age, string grade, string setup)
{
   string sym = MapSymbol(srcSym);
   string tag  = StringFormat("signal #%I64d %s %s (grade %s, %s, %ds old)", id, side, srcSym, grade, setup, age);
   if(sym == "") { Print("SKIP ", tag, ": no entry for ", srcSym, " in SymbolMap"); return; }
   if(!SymbolSelect(sym, true)) { Print("SKIP ", tag, ": symbol ", sym, " not found at this broker"); return; }
   if(age > MaxSignalAgeSec)    { Print("SKIP ", tag, ": older than ", MaxSignalAgeSec, "s"); return; }
   if(MaxTradesPerDay > 0 && TradesToday() >= MaxTradesPerDay) { Print("SKIP ", tag, ": daily trade limit reached"); return; }
   double lossLimit = AccountInfoDouble(ACCOUNT_BALANCE) * MaxDailyLossPct / 100.0;
   if(MaxDailyLossPct > 0 && PnlToday() <= -lossLimit) { Print("SKIP ", tag, ": daily loss limit reached"); return; }
   if(HaveOpenPosition(sym)) { Print("SKIP ", tag, ": already in a trade on ", sym); return; }

   long spread = SymbolInfoInteger(sym, SYMBOL_SPREAD);
   if(MaxSpreadPoints > 0 && spread > MaxSpreadPoints) { Print("SKIP ", tag, ": spread ", spread, " points"); return; }

   bool buy   = (side == "long");
   double ask = SymbolInfoDouble(sym, SYMBOL_ASK), bid = SymbolInfoDouble(sym, SYMBOL_BID);
   double px  = buy ? ask : bid;
   int    dg  = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double pt  = SymbolInfoDouble(sym, SYMBOL_POINT);
   double minStop = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL) * pt;
   if(slD < minStop || tp2D < minStop) { Print("SKIP ", tag, ": stop/target closer than broker minimum"); return; }

   double sl  = NormalizeDouble(buy ? px - slD  : px + slD,  dg);
   double tp  = NormalizeDouble(buy ? px + tp2D : px - tp2D, dg);
   double tp1 = NormalizeDouble(buy ? px + tp1D : px - tp1D, dg);
   double lots = LotsForRisk(sym, slD);
   if(lots <= 0) { Print("SKIP ", tag, ": lot size for ", RiskPercent, "% risk is below the broker minimum"); return; }

   Print(DryRun ? "DRY-RUN would " : "PLACING ", buy ? "BUY " : "SELL ", sym, " lots=", DoubleToString(lots, 2),
         " price=", DoubleToString(px, dg), " SL=", DoubleToString(sl, dg), " TP1=", DoubleToString(tp1, dg),
         " TP2=", DoubleToString(tp, dg), " | ", tag);
   if(DryRun) return;

   trade.SetTypeFillingBySymbol(sym);
   bool ok = buy ? trade.Buy(lots, sym, 0, sl, tp, "AS#" + (string)id)
                 : trade.Sell(lots, sym, 0, sl, tp, "AS#" + (string)id);
   if(!ok) { Print("ORDER FAILED: ", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription()); return; }

   // remember TP1 for the partial close / break-even step
   ulong deal = trade.ResultDeal();
   if(deal > 0 && HistoryDealSelect(deal))
   {
      ulong posId = (ulong)HistoryDealGetInteger(deal, DEAL_POSITION_ID);
      GlobalVariableSet("ASEA_tp1_" + (string)posId, tp1);
   }
}

//------------------------------------------------------------------
void ManagePositions()
{
   if(DryRun) return;
   for(int j = PositionsTotal() - 1; j >= 0; j--)
   {
      ulong tk = PositionGetTicket(j);
      if(tk == 0 || PositionGetInteger(POSITION_MAGIC) != (long)MagicNumber) continue;
      string sym = PositionGetString(POSITION_SYMBOL);
      ulong  pid = (ulong)PositionGetInteger(POSITION_IDENTIFIER);
      string gTp = "ASEA_tp1_" + (string)pid, gDone = "ASEA_done_" + (string)pid;
      if(!GlobalVariableCheck(gTp) || GlobalVariableCheck(gDone)) continue;

      bool   buy = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
      double tp1 = GlobalVariableGet(gTp);
      double cur = buy ? SymbolInfoDouble(sym, SYMBOL_BID) : SymbolInfoDouble(sym, SYMBOL_ASK);
      if(buy ? cur < tp1 : cur > tp1) continue;       // TP1 not reached yet

      double vol = PositionGetDouble(POSITION_VOLUME), step = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
      double vmin = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
      double part = MathFloor(vol * PartialPercent / 100.0 / step) * step;
      if(PartialPercent > 0 && part >= vmin && vol - part >= vmin)
         if(!trade.PositionClosePartial(tk, part)) Print("Partial close failed: ", trade.ResultRetcodeDescription());

      if(MoveToBreakEven && PositionSelectByTicket(tk))
      {
         double open = PositionGetDouble(POSITION_PRICE_OPEN), tp = PositionGetDouble(POSITION_TP);
         double curSl = PositionGetDouble(POSITION_SL);
         bool better = buy ? (curSl < open) : (curSl > open || curSl == 0);
         if(better && !trade.PositionModify(tk, open, tp)) Print("Break-even move failed: ", trade.ResultRetcodeDescription());
      }
      GlobalVariableSet(gDone, 1);
   }
}
//+------------------------------------------------------------------+
