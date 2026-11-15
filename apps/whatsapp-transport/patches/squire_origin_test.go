package store

import (
 "testing"
 "time"
)

func TestSquireOriginIsAtomicAndHistoryCannotGainLiveOrigin(t *testing.T) {
 db := openTestDB(t)
 chat := "123@s.whatsapp.net"
 if err := db.UpsertChat(chat,"dm","fixture",time.Now()); err != nil { t.Fatal(err) }
 p := UpsertMessageParams{ChatJID:chat, MsgID:"one", Timestamp:time.Now(), Text:"live", SquireLiveOrigin:true}
 if err := db.UpsertMessage(p); err != nil { t.Fatal(err) }
 var live int
 if err := db.sql.QueryRow(`SELECT live FROM squire_change_origins ORDER BY sequence DESC LIMIT 1`).Scan(&live); err != nil || live != 1 { t.Fatalf("live origin=%d error=%v",live,err) }
 p.Text="late history"
 p.SquireLiveOrigin=false
 if err := db.UpsertMessage(p); err != nil { t.Fatal(err) }
 if err := db.sql.QueryRow(`SELECT live FROM squire_change_origins ORDER BY sequence DESC LIMIT 1`).Scan(&live); err != nil || live != 0 { t.Fatalf("history origin=%d error=%v",live,err) }
 if _, err := db.sql.Exec(`CREATE TRIGGER squire_fail_origin BEFORE INSERT ON squire_change_origins BEGIN SELECT RAISE(ABORT,'injected origin failure'); END`); err != nil { t.Fatal(err) }
 p.MsgID="rollback"
 if err := db.UpsertMessage(p); err == nil { t.Fatal("expected origin failure") }
 if got := countRows(t,db.sql,`SELECT COUNT(*) FROM messages WHERE msg_id='rollback'`); got != 0 { t.Fatal("message committed without provenance") }
}
