package store

import (
	"database/sql"
	"testing"
	"time"
)

func TestSquireExpiryMinimumReplayAtomicAndIdentity(t *testing.T) {
	db := openTestDB(t)
	chat := "123@lid"
	pn := "15555550123@s.whatsapp.net"
	if err := db.UpsertChat(chat, "dm", "fixture", time.Now()); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Unix() + 60
	p := UpsertMessageParams{ChatJID: chat, MsgID: "ttl", Timestamp: time.Now(), Text: "private", SquireExpiryKnown: true, SquireExpiresAt: &deadline}
	if err := db.UpsertMessage(p); err != nil {
		t.Fatal(err)
	}
	later := deadline + 500
	p.SquireExpiresAt = &later
	if err := db.UpsertMessage(p); err != nil {
		t.Fatal(err)
	}
	p.SquireExpiresAt = nil
	if err := db.UpsertMessage(p); err != nil {
		t.Fatal(err)
	}
	if err := db.MigrateLIDToPN(chat, pn); err != nil {
		t.Fatal(err)
	}
	var got int64
	var known int
	if err := db.sql.QueryRow(`SELECT known,expires_at FROM squire_message_expiry WHERE chat_jid=? AND msg_id='ttl'`, pn).Scan(&known, &got); err != nil || got != deadline || known != 1 {
		t.Fatalf("lost minimum identity expiry %d %d %v", known, got, err)
	}
	p.ChatJID = pn
	earlier := deadline - 1
	p.SquireExpiresAt = &earlier
	var before, after int64
	db.sql.QueryRow(`SELECT max(sequence) FROM message_changes`).Scan(&before)
	if err := db.UpsertMessage(p); err != nil {
		t.Fatal(err)
	}
	db.sql.QueryRow(`SELECT max(sequence) FROM message_changes`).Scan(&after)
	if after <= before {
		t.Fatal("expiry-only update not journaled")
	}
	if _, err := db.sql.Exec(`CREATE TRIGGER fail_expiry BEFORE UPDATE ON squire_message_expiry BEGIN SELECT RAISE(ABORT,'injected'); END`); err != nil {
		t.Fatal(err)
	}
	p.Text = "must rollback"
	if err := db.UpsertMessage(p); err == nil {
		t.Fatal("expected abort")
	}
	m, err := db.GetMessage(pn, "ttl")
	if err != nil || m.Text != "private" {
		t.Fatal("non-atomic expiry")
	}
}

func TestSquireExpiryUnknownCannotBecomeOrdinary(t *testing.T) {
	db := openTestDB(t)
	chat := "123@s.whatsapp.net"
	db.UpsertChat(chat, "dm", "fixture", time.Now())
	p := UpsertMessageParams{ChatJID: chat, MsgID: "unknown", Timestamp: time.Now(), Text: "legacy"}
	if err := db.UpsertMessage(p); err != nil {
		t.Fatal(err)
	}
	p.SquireExpiryKnown = true
	if err := db.UpsertMessage(p); err != nil {
		t.Fatal(err)
	}
	var known int
	var expiry sql.NullInt64
	if err := db.sql.QueryRow(`SELECT known,expires_at FROM squire_message_expiry WHERE msg_id='unknown'`).Scan(&known, &expiry); err != nil || known != 0 || expiry.Valid {
		t.Fatal("legacy upgraded to ordinary")
	}
}

func TestSquireExpiryLegacyOpenPreservesJournal(t *testing.T) {
	db := openTestDB(t)
	chat := "123@s.whatsapp.net"
	db.UpsertChat(chat, "dm", "fixture", time.Now())
	if _, err := db.sql.Exec(`INSERT INTO messages(chat_jid,msg_id,ts,from_me,text) VALUES (?,'old',1,0,'legacy')`, chat); err != nil {
		t.Fatal(err)
	}
	var storeID string
	var sequence int64
	db.sql.QueryRow(`SELECT store_id FROM retrieval_state`).Scan(&storeID)
	db.sql.QueryRow(`SELECT max(sequence) FROM message_changes`).Scan(&sequence)
	path := db.path
	if _, err := db.sql.Exec(`DROP TABLE squire_message_expiry`); err != nil {
		t.Fatal(err)
	}
	db.Close()
	reopened, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	var actualID string
	var actualSequence int64
	var known int
	reopened.sql.QueryRow(`SELECT store_id FROM retrieval_state`).Scan(&actualID)
	reopened.sql.QueryRow(`SELECT max(sequence) FROM message_changes`).Scan(&actualSequence)
	if err := reopened.sql.QueryRow(`SELECT known FROM squire_message_expiry WHERE msg_id='old'`).Scan(&known); err != nil || known != 0 {
		t.Fatal("legacy not quarantined")
	}
	if storeID != actualID || sequence != actualSequence {
		t.Fatal("legacy metadata changed capture identity/checkpoint")
	}
}
