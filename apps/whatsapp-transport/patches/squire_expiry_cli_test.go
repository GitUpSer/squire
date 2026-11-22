package main

import (
	"context"
	"github.com/openclaw/wacli/internal/lock"
	"testing"
	"time"
)

func TestSquireExpiryAbsoluteDeadlineReachesDelegate(t *testing.T) {
	skipPresenceDelegateSocketTestOnUnsupportedOS(t)
	dir := shortPresenceDelegateStoreDir(t)
	lk, err := lock.Acquire(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer lk.Release()
	server := startPresenceDelegateTestSocket(t, dir, func(req sendDelegateRequest) sendDelegateResponse {
		return sendDelegateResponse{OK: true, Sent: true, ID: "synthetic"}
	})
	defer server.stop()
	expires := time.Now().Unix() + 10
	flags := &rootFlags{storeDir: dir, timeout: time.Minute, squireExpiresAt: expires}
	if _, err := delegateSend(context.Background(), flags, sendDelegateRequest{Kind: "text"}); err != nil {
		t.Fatal(err)
	}
	req := server.nextRequest(t)
	if req.DeadlineUnixMS != expires*1000 {
		t.Fatal("expiry extended by subprocess/queue")
	}
	ctx, cancel := withTimeout(context.Background(), flags)
	defer cancel()
	deadline, ok := ctx.Deadline()
	if !ok || deadline.Unix() != expires {
		t.Fatal("direct send lost expiry")
	}
}
