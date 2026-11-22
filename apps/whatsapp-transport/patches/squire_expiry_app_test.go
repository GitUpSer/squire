package app

import (
	"database/sql"
	waProto "go.mau.fi/whatsmeow/binary/proto"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"
	"path/filepath"
	"testing"
	"time"
)

func TestSquireExpiryProviderLiveStore(t *testing.T) {
	for _, tc := range []struct {
		name    string
		seconds uint32
		age     int
		view    bool
		media   bool
		known   int
		blocked bool
	}{
		{"ordinary", 0, 0, false, false, 1, false}, {"short_text", 60, 0, false, false, 1, false},
		{"short_image", 60, 0, false, true, 1, false}, {"expired", 60, 120, false, true, 1, true},
		{"unknown", 0, 0, false, true, 0, true}, {"view_once", 60, 0, true, true, 0, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			a := newTestApp(t)
			a.wa = newFakeWA()
			chat := types.JID{User: "15555550123", Server: types.DefaultUserServer}
			at := time.Unix(time.Now().Unix()-int64(tc.age), 0)
			context := &waProto.ContextInfo{Expiration: proto.Uint32(tc.seconds)}
			inner := &waProto.Message{ExtendedTextMessage: &waProto.ExtendedTextMessage{Text: proto.String("synthetic secret"), ContextInfo: context}}
			if tc.media {
				inner = &waProto.Message{ImageMessage: &waProto.ImageMessage{Caption: proto.String("synthetic secret"), ContextInfo: context, Mimetype: proto.String("image/png"), FileLength: proto.Uint64(4), DirectPath: proto.String("/synthetic"), MediaKey: []byte{1, 2}}}
			}
			raw := &waProto.Message{EphemeralMessage: &waProto.FutureProofMessage{Message: inner}}
			if tc.name == "ordinary" {
				raw = inner
			}
			if tc.view {
				raw = &waProto.Message{ViewOnceMessageV2: &waProto.FutureProofMessage{Message: inner}}
			}
			evt := liveMessage(chat, "synthetic", false, at, nil)
			evt.RawMessage = raw
			evt = evt.UnwrapRaw()
			feedLive(t, a, evt)
			db, err := sql.Open("sqlite3", filepath.Join(a.StoreDir(), "wacli.db"))
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			var known int
			var expiry sql.NullInt64
			if err = db.QueryRow(`SELECT known,expires_at FROM squire_message_expiry WHERE msg_id='synthetic'`).Scan(&known, &expiry); err != nil {
				t.Fatal(err)
			}
			if known != tc.known || expiry.Valid != (tc.seconds > 0) || expiry.Valid && expiry.Int64 != at.Unix()+int64(tc.seconds) {
				t.Fatalf("wrong expiry metadata: %d %+v", known, expiry)
			}
			stored, err := a.db.GetMessage(chat.String(), "synthetic")
			if err != nil {
				t.Fatal(err)
			}
			if tc.blocked && (stored.Text != "" || stored.MediaType != "") {
				t.Fatal("blocked content stored")
			}
			if !tc.blocked && !tc.media && stored.Text != "synthetic secret" {
				t.Fatal("ordinary/short content lost")
			}
		})
	}
}
