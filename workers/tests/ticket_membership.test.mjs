import { DatabaseSync } from "node:sqlite";
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { getPicksPage } from "../src/db.ts";

function fixture() {
  const sql = new DatabaseSync(":memory:");
  sql.exec(`CREATE TABLE picks(id INTEGER PRIMARY KEY,pick_date TEXT,tier TEXT,rank INTEGER,
    sport TEXT,league TEXT,home TEXT,away TEXT,start_utc TEXT,market TEXT,
    period TEXT DEFAULT 'full',stat TEXT DEFAULT 'points',entity TEXT DEFAULT 'all',line REAL DEFAULT 2.5,selection TEXT,
    label TEXT,price REAL,prob REAL,status TEXT);
    CREATE TABLE prediction_memberships(prediction_id INTEGER REFERENCES picks(id),
      collection TEXT,rank INTEGER,PRIMARY KEY(prediction_id,collection));`);
  const insert = sql.prepare(
    "INSERT INTO picks(id,pick_date,tier,rank,sport,league,home,away,start_utc,market,selection,label,price,prob,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
  );
  for (const [id, tier, rank, day] of [[1,"all",99,"2030-06-01"], [2,"daily",3,"2030-06-01"],
    [3,"top",88,"2030-06-01"], [4,"all",1,"2030-05-31"], [5,"all",4,"2030-06-01"],
    [6,"all",66,"2030-06-01"]]) {
    insert.run(id,day,tier,rank,"football","League",`Home${id}`,`Away${id}`,"2030-06-01T22:00:00Z",
      "total","over",`Over ${id === 5 ? 2.5 : 1.5}`,1.5,.8,"pending");
  }
  sql.exec(`INSERT INTO prediction_memberships VALUES(1,'top',1),(1,'daily',1),(1,'all',1),
    (3,'all',2),(4,'top',0),(5,'all',4),(6,'top',2);`);
  const db = { prepare(query) { return { bind(...args) {
    return { async all() { return { results: sql.prepare(query).all(...args) }; } };
  } }; } };
  return {sql,db};
}

describe("ticket memberships against real SQLite", () => {
  it("filters invalid cached sport-market rows while retaining supported moneylines and soccer DC", async () => {
    const {sql,db} = fixture();
    try {
      const insert = sql.prepare(
        "INSERT INTO picks(id,pick_date,tier,rank,sport,league,home,away,start_utc,market,selection,label,price,prob,status,period,stat,entity,line) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      );
      const rows = [
        [10,"american_football","dc","home_draw"],
        [11,"baseball","dc","home_draw"],
        [12,"basketball","result2","home"],
        [13,"baseball","result2","away"],
        [14,"football","dc","home_draw"],
      ];
      for (const [id,sport,market,selection] of rows) {
        insert.run(id,"2030-06-01","top",id,sport,"League",`Home${id}`,`Away${id}`,
          "2030-06-01T22:00:00Z",market,selection,"Pick",1.8,.8,"pending","full","points","all",null);
        sql.prepare("INSERT INTO prediction_memberships VALUES(?,?,?)").run(id,"top",id);
      }
      const result = await getPicksPage(db,"2030-06-01","top",25,0);
      const displayed = new Set(result.map((pick) => pick.id));
      assert.equal(displayed.has(10), false);
      assert.equal(displayed.has(11), false);
      assert.deepEqual([12,13,14].map((id) => displayed.has(id)), [true,true,true]);
    } finally { sql.close(); }
  });

  it("serves promoted alternates even though their immutable tier is all", async () => {
    const {sql,db} = fixture();
    try {
      const top = await getPicksPage(db,"2030-06-01","top",25,0);
      assert.deepEqual(top.map(p => [p.id,p.tier,p.rank]), [[1,"top",1],[6,"top",2]]);
      assert.equal(sql.prepare("SELECT tier,rank FROM picks WHERE id=1").get().tier,"all");
      assert.deepEqual((await getPicksPage(db,"2030-06-01","all",25,0)).map(p=>p.id),[1,3,2,5]);
    } finally { sql.close(); }
  });
  it("includes unconverted legacy rows without reintroducing stale tier memberships", async () => {
    const {sql,db} = fixture();
    try {
      const daily = await getPicksPage(db,"2030-06-01","daily",25,0);
      assert.deepEqual(daily.map(p=>p.id),[1,2]);
      assert.equal(daily[1].tier,"daily");
      assert.equal(sql.prepare("SELECT count(*) AS n FROM picks").get().n,6);
    } finally { sql.close(); }
  });
  it("paginates by collection rank and excludes other publication dates", async () => {
    const {sql,db} = fixture();
    try {
      assert.deepEqual((await getPicksPage(db,"2030-06-01","top",1,1)).map(p=>p.id),[6]);
      assert.deepEqual(await getPicksPage(db,"2030-06-01","top",1,2),[]);
    } finally { sql.close(); }
  });
});
