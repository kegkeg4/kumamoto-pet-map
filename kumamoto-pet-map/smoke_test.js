// 全ビューのDOMスモークテスト: 各ルートを実際に描画し、実行時エラーを検出する
const { JSDOM } = require("jsdom");
const fs = require("fs");

const html = fs.readFileSync("static/index.html", "utf8");
// 外部スクリプト(Leaflet CDN)は読み込まない。インラインJSは自前で実行。
const inline = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const shell = html.replace(/<script src=[\s\S]*?<\/script>/, "").replace(/<script>[\s\S]*?<\/script>/, "");

const dom = new JSDOM(shell, { url: "http://localhost/", runScripts: "outside-only", pretendToBeVisual: true });
const w = dom.window;

// ---- Leafletモック ----
function chain(extra){ const o = {...extra}; ["addTo","bindTooltip","bindPopup","setView","setLatLng"].forEach(k=>{ if(!o[k]) o[k]=()=>o; }); return o; }
const mapObj = () => chain({
  on: ()=>{}, remove: ()=>{}, invalidateSize: ()=>{},
  dragging: {disable(){}, enable(){}}, scrollWheelZoom: {disable(){}, enable(){}},
  containerPointToLatLng: ()=>({lat:32.8, lng:130.7}),
});
w.L = {
  map: () => mapObj(),
  tileLayer: () => chain({}),
  marker: () => chain({}),
  circleMarker: () => chain({}),
  circle: () => chain({}),
  rectangle: () => chain({options:{weight:0}}),
  layerGroup: () => chain({removeLayer(){}}),
  point: (x,y)=>({x,y}),
};
w.navigator.clipboard = { writeText: () => Promise.resolve() };
w.alert = ()=>{}; w.confirm = ()=>true; w.prompt = ()=>"テスト";

// ---- fetchモック(APIレスポンス) ----
const samplePet = (over={}) => Object.assign({
  id:"abcd1234", kind:"lost", species:"cat", name:"タマ", breed:"キジトラ", size:"medium",
  color:"茶縞", features:"しっぽ先が白い", event_at:"2026-07-28T17:00", lat:32.803, lng:130.708,
  address:"中央区新市街", collar:true, microchip:false, shelter_info:"", photos:[],
  status:"searching", created_at:"2026-07-28T18:00", contact:"090-0000-0000", contact_public:true,
  sighting_count:1,
}, over);
w.fetch = (url, opts) => {
  let body = {ok:true};
  url = String(url);
  if(url.startsWith("/api/stats")) body = {lost_active:2, found_active:1, reunited:1};
  else if(url.startsWith("/api/pets?")) body = {pets:[samplePet(), samplePet({id:"efgh5678",kind:"found",status:"sheltering",name:""})]};
  else if(/^\/api\/pets\/[a-z0-9]{8}$/.test(url.split("?")[0])) body = Object.assign(samplePet(), {
    sightings:[{id:1,lat:32.804,lng:130.709,seen_at:"2026-07-30T22:00",memo:"車の下"}],
    searched:[{cell:"24298_80684",searched_at:"2026-07-31T01:00"}],
    updates:[{body:"夜間に捜索します",created_at:"2026-07-31T02:00"}],
    similar:[samplePet({id:"efgh5678",kind:"found",status:"sheltering"})],
  });
  else if(url.startsWith("/api/admin/list")) body = {pets:[{id:"abcd1234",kind:"lost",species:"dog",name:"モカ",color:"茶",address:"",status:"searching",flags:3,hidden:1,created_at:""}], sightings:[]};
  else if(url.startsWith("/api/generate")) body = {x:"テスト投稿", long:"テスト長文", flyer_catch:"さがしています", flyer_note:"注意", source:"template"};
  return Promise.resolve({ ok:true, status:200, json: () => Promise.resolve(body) });
};

// ---- インラインJSを実行 ----
let errors = [];
w.addEventListener("error", e => errors.push("window.onerror: " + e.message));
try { w.eval(inline); } catch(e){ errors.push("初期実行: " + e.message); }

const routes = ["#/", "#/register", "#/register/found", "#/info",
  "#/pet/abcd1234", "#/pet/abcd1234?token=xyz", "#/flyer/abcd1234",
  "#/done/abcd1234/tok123", "#/terms", "#/admin"];
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  for(const r of routes){
    try {
      w.location.hash = r;
      w.dispatchEvent(new w.Event("hashchange"));
      await sleep(60); // 非同期描画待ち
      const len = w.document.getElementById("app").innerHTML.length;
      if(len < 50) errors.push(r + ": 描画が空(len=" + len + ")");
      else console.log("OK " + r + " (描画 " + len + "文字)");
    } catch(e){ errors.push(r + ": " + e.message); }
  }
  // 管理画面: キー入力後の一覧描画
  try {
    w.sessionStorage.setItem("kpm-admin-key","testkey");
    w.location.hash = "#/admin2"; w.dispatchEvent(new w.Event("hashchange")); // 一旦別ルート
    w.location.hash = "#/admin"; w.dispatchEvent(new w.Event("hashchange"));
    await sleep(60);
    const html2 = w.document.getElementById("app").innerHTML;
    if(html2.includes("運営管理") && html2.includes("モカ")) console.log("OK #/admin(キーあり・一覧描画)");
    else errors.push("#/admin キーあり: 一覧が描画されない");
  } catch(e){ errors.push("#/admin キーあり: " + e.message); }

  if(errors.length){ console.log("\n=== エラー ==="); errors.forEach(e=>console.log("NG " + e)); process.exit(1); }
  console.log("\n全ルート スモークテスト合格 🎉");
})();
