"""Admission must use the row protected by its lock, not an older identity-map copy."""
from datetime import timedelta
import pytest
from sqlmodel import Session,select
from control_api.db import get_engine
from control_api.models import Tenant,utcnow
from control_api.pilot_models import PilotEntitlement,PilotRuntime,PilotTurn
from control_api.pilot_lifecycle import admit_turn


def seed():
    with Session(get_engine()) as session:
        session.add(Tenant(id="owner",email="owner@example.com"));session.flush()
        session.add(PilotEntitlement(tenant_id="owner",state="active",activated_at=utcnow()-timedelta(minutes=5),expires_at=utcnow()+timedelta(hours=71)))
        session.add(PilotRuntime(id="owner",owner_tenant_id="owner",scope_id="owner-dm",ready=True,registered=True,model_ready=True,state="ready"))
        session.commit()


@pytest.mark.parametrize("change,reason",[("spent_quota","daily_limit"),("paused_entitlement","paused"),("paused_runtime","paused")])
def test_admission_refreshes_preloaded_rows_after_lock(change,reason):
    seed()
    with Session(get_engine()) as older:
        cached_ent=older.get(PilotEntitlement,"owner")
        cached_runtime=older.get(PilotRuntime,"owner")
        assert cached_ent.daily_turns==0 and cached_runtime.state=="ready"
        with Session(get_engine()) as writer:
            ent=writer.get(PilotEntitlement,"owner");runtime=writer.get(PilotRuntime,"owner")
            if change=="spent_quota":ent.daily_turns=75;ent.lifetime_turns=75
            elif change=="paused_entitlement":ent.state="paused"
            else:runtime.state="paused"
            writer.add(ent);writer.add(runtime);writer.commit()
        decision=admit_turn(older,"owner","owner","after-change")
        assert decision["allowed"] is False and decision["reason"]==reason
    with Session(get_engine()) as session:
        assert session.exec(select(PilotTurn)).all()==[]
        if change=="spent_quota":assert session.get(PilotEntitlement,"owner").daily_turns==75
