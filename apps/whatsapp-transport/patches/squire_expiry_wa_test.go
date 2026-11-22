package wa

import (
	waProto "go.mau.fi/whatsmeow/binary/proto"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
	"testing"
	"time"
)

func TestSquireExpiryHistoryAndMalformed(t *testing.T) {
	now := time.Now().Unix()
	hist := &waProto.WebMessageInfo{MessageTimestamp: proto.Uint64(uint64(now)), EphemeralStartTimestamp: proto.Uint64(uint64(now - 20)), EphemeralDuration: proto.Uint32(60), Message: &waProto.Message{Conversation: proto.String("history")}}
	got := ParseHistoryMessage("123@s.whatsapp.net", hist)
	if !got.SquireExpiryKnown || got.SquireExpiresAt == nil || *got.SquireExpiresAt != now+40 {
		t.Fatal("history expiry lost")
	}
	hist.EphemeralDuration = nil
	got = ParseHistoryMessage("123@s.whatsapp.net", hist)
	if got.SquireExpiryKnown || got.Text != "" {
		t.Fatal("unknown history disclosed")
	}
	raw := &waProto.Message{EphemeralMessage: &waProto.FutureProofMessage{}}
	got = ParseLiveMessage((&events.Message{RawMessage: raw}).UnwrapRaw())
	if got.SquireExpiryKnown || got.Text != "" {
		t.Fatal("malformed envelope treated as ordinary")
	}
}
