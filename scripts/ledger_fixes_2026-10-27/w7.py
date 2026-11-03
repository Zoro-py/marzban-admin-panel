import asyncio, sqlite3, time
from datetime import datetime
from sqlmodel import Session, select
from app.db import engine
from app.models import *
from app.schemas import AccountRelationshipUpdate, AccountBillingUpdate, LedgerCreate
from app.services import MoneyBook, roll_usage_baseline_on_payg_entry
from app.statement import build_statement
from app.routers.accounts import update_relationship, update_billing, set_accounting_synced
from app.routers.ledger import _write_entry
from app.schemas import AccountingSyncedUpdate
OP="claude:syhs-payg-2026-10-27"; AID=89; RATE=5000.0
async def main():
    s0=sqlite3.connect("file:/app/data/vpn.db?mode=ro",uri=True); d=sqlite3.connect("/app/data/manual_backups/pre-syhs-%s.db"%time.strftime("%Y%m%d-%H%M%S")); s0.backup(d); d.close(); print("backup ok")
    with Session(engine) as s:
        since=datetime(2026,7,10)
        r=await build_statement(s,since=since,account_id=AID,with_usage=True)
        used=r["volume"]["used_gb"]; charged=r["statement"]["charged"]
        target=round(used*RATE); adj=target-round(charged)
        print("used since 8/12",used,"target",target,"charged post 8/12",charged,"adj",adj)
        await update_relationship(AID,AccountRelationshipUpdate(group_id=None),s,OP)
        await update_billing(AID,AccountBillingUpdate(rate_per_gb=RATE),s,OP)
        a=s.get(Account,AID); a.billing_mode=BillingMode.payg; s.add(a)
        roll_usage_baseline_on_payg_entry(s,a,reason="syhs re-based to payg 5000/GB (group rate typo 50)",created_by=OP)
        s.add(AccountEvent(account_id=AID,action="billing_change",detail="payg 5000/GB; left group seyed (rate typo 50); billed on exact Marzban usage since account start 2026-07-10",created_by=OP)); s.commit()
        _write_entry(LedgerCreate(type=LedgerType.charge if adj>0 else LedgerType.credit,amount=abs(float(adj)),customer_id=a.customer_id,account_id=AID,note=f"Payg re-basing 2026-10-27: Marzban consumption since account start 2026-07-10 (exact) {used} GB x {RATE:.0f} = {target:,} minus charges already posted since {charged:,.0f}"),s,OP)
        s.add(LedgerEntry(type=LedgerType.credit,amount=50000.0,customer_id=a.customer_id,account_id=AID,date=datetime(2026,8,11,20,31),note="payment for first 10 GB (settlement 2026-08-11), owner statement 2026-10-27",source=LedgerSource.web,created_by=OP)); s.commit()
        await set_accounting_synced(AID,AccountingSyncedUpdate(synced=True),s,OP)
        s.expire_all(); b=MoneyBook(s)
        a=s.get(Account,AID); print("acc89",a.billing_mode.value,a.group_id,a.rate_per_gb,"pending",b.account_pending(a),"posted",b.account_posted(a))
        for c in (81,60): print("cust",c,round(b.customer_net(s.get(Customer,c)),2))
asyncio.run(main())
