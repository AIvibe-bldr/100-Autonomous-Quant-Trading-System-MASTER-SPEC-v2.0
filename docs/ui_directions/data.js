// SAMPLE data shared by the three direction mockups (not real). Same facts, three ways of showing them.
const SAMPLE = (() => {
  const H = [
    ["PLTR","Palantir","データ分析・AI",0.402,359.83,369.90,332.20,404.75,"1w","市場に連れ高。20日高値更新を狙う"],
    ["NVDA","NVIDIA","AI・半導体",0.241,146.83,151.20,136.40,168.00,"1w","出来高が平均の1.7倍に増加"],
    ["TSLA","Tesla","電気自動車",0.310,342.10,339.80,318.50,372.00,"1w","20日モメンタム+10%"],
    ["CVX","Chevron","エネルギー（石油）",0.723,138.86,139.07,129.40,151.00,"1w","資源高に連動、配当も下支え"],
    ["PANW","Palo Alto Networks","サイバーセキュリティ",0.055,1053.06,1030.20,975.00,1130.00,"1w","セクター全体の強さに追随"],
    ["GOOG","Alphabet (Google)","検索・AI",0.140,278.01,269.40,258.00,301.00,"1w","AI関連の資金流入"],
    ["AVGO","Broadcom","AI・半導体",0.158,250.16,254.46,231.24,287.86,"1w","半導体の押し目"],
    ["AMD","AMD","AI・半導体",0.035,1029.39,1073.49,944.58,1198.38,"1w","市場に連れ高"],
    ["MSFT","Microsoft","AI・ソフトウェア",0.190,498.20,503.10,471.00,540.00,"1w","安定したモメンタム"],
    ["XOM","Exxon Mobil","エネルギー（石油）",0.610,118.40,117.35,111.00,128.00,"1w","資源高に連動"],
  ].map(([sym,co,cat,qty,cost,price,stop,target,hz,why]) => {
    const value = qty*price, pnl = qty*(price-cost);
    return {sym,co,cat,qty,cost,price,stop,target,hz,why,value,pnl,pct:price/cost-1,
            toStop:price/stop-1,toTarget:target/price-1,pos:(price-stop)/(target-stop)};
  });
  const cash = 102.70, k = (672.85-cash)/H.reduce((t,h)=>t+h.value,0);      // scale so the sample equity is $672.85
  H.forEach(h=>{h.qty*=k;h.value*=k;h.pnl*=k});
  const equity = H.reduce((t,h)=>t+h.value,0)+cash;
  H.forEach(h=>h.weight=h.value/equity);
  const NEAR = 0.06, near = H.filter(h=>h.toStop<NEAR).length;
  const eq = [670,668.8,667.9,669.5,671.2,670.4,668.1,669.9,672.0,671.1,669.2,670.8,672.3,671.5,669.9,670.6,672.85];
  return {NEAR, H, cash, equity, start:670, eq, max_dd:0.0112, sessions:17, need:60, date:"8/14", next:"11/10（火）23:45",
          status:{word:"正常",head:"正常に動いています。",note:`持っている10銘柄のうち、${near}銘柄が損切りラインに近づいています。`}};
})();
const $ = (id)=>document.getElementById(id);
const usd = (x,s)=> (s&&x>0?"+":"")+(x<0?"-":"")+"$"+Math.abs(x).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
const pc = (x,d=2,s=true)=> (s&&x>0?"+":"")+(x*100).toFixed(d)+"%";
const sgn = (x)=> x>0?"up":x<0?"dn":"fl";
function spark(vals,w,h,color){const lo=Math.min(...vals),hi=Math.max(...vals),p=(hi-lo)*.1||1;
  const x=i=>2+(w-4)*i/(vals.length-1),y=v=>h-2-(h-4)*(v-lo+p)/(hi-lo+2*p);
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" preserveAspectRatio="none"><polyline fill="none" stroke="${color}" stroke-width="1.6" points="${vals.map((v,i)=>x(i)+","+y(v)).join(" ")}"/></svg>`;}
