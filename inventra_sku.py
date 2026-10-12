"""
INVENTRA item classification - the one rule every module uses
==============================================================
Only items whose SKU starts with "INV_" are inventory (stock held for
resale). Everything else is not inventory and never counts towards stock on
hand, inventory value or cost of sales:

  FA_...    fixed assets      - balance sheet at book value; depreciation is
                                 an expense
  OFF_...   office supplies   - an operating (admin) expense when bought
  other     non-inventory      - treated like office supplies (expensed)
"""

INVENTORY_PREFIX = "INV_"
FIXED_ASSET_PREFIX = "FA_"
OFFICE_SUPPLY_PREFIX = "OFF_"


def _norm(sku) -> str:
    return str(sku or "").strip().upper()


def sku_kind(sku) -> str:
    """'inventory', 'fixed_asset', 'office_supply' or 'other'."""
    s = _norm(sku)
    if s.startswith(INVENTORY_PREFIX):
        return "inventory"
    if s.startswith(FIXED_ASSET_PREFIX) or s.startswith("FA-"):
        return "fixed_asset"
    if s.startswith(OFFICE_SUPPLY_PREFIX) or s.startswith("OFF-"):
        return "office_supply"
    return "other"


def is_inventory_sku(sku) -> bool:
    return _norm(sku).startswith(INVENTORY_PREFIX)
