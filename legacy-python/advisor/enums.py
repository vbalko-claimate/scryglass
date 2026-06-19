"""MTGA enum mappings."""

ZONE_TYPES = {
    "ZoneType_Hand": "Hand",
    "ZoneType_Library": "Library",
    "ZoneType_Battlefield": "Battlefield",
    "ZoneType_Graveyard": "Graveyard",
    "ZoneType_Stack": "Stack",
    "ZoneType_Exile": "Exile",
    "ZoneType_Limbo": "Limbo",
    "ZoneType_Command": "Command",
    "ZoneType_Sideboard": "Sideboard",
    "ZoneType_Revealed": "Revealed",
    "ZoneType_Pending": "Pending",
    "ZoneType_Suppressed": "Suppressed",
}

PHASES = {
    "Phase_Beginning": "Beginning",
    "Phase_Main1": "Main 1",
    "Phase_Combat": "Combat",
    "Phase_Main2": "Main 2",
    "Phase_Ending": "Ending",
}

STEPS = {
    "Step_Untap": "Untap",
    "Step_Upkeep": "Upkeep",
    "Step_Draw": "Draw",
    "Step_BeginCombat": "Begin Combat",
    "Step_DeclareAttack": "Declare Attackers",
    "Step_DeclareBlock": "Declare Blockers",
    "Step_FirstStrikeDamage": "First Strike Damage",
    "Step_CombatDamage": "Combat Damage",
    "Step_EndCombat": "End Combat",
    "Step_EndStep": "End Step",
    "Step_Cleanup": "Cleanup",
}

CARD_COLORS = {
    "CardColor_White": "W",
    "CardColor_Blue": "U",
    "CardColor_Black": "B",
    "CardColor_Red": "R",
    "CardColor_Green": "G",
    "CardColor_Colorless": "C",
}

CARD_TYPES = {
    "CardType_Artifact": "Artifact",
    "CardType_Creature": "Creature",
    "CardType_Enchantment": "Enchantment",
    "CardType_Instant": "Instant",
    "CardType_Land": "Land",
    "CardType_Planeswalker": "Planeswalker",
    "CardType_Sorcery": "Sorcery",
    "CardType_Battle": "Battle",
}

ACTION_TYPES = {
    "ActionType_Play": "Play",
    "ActionType_Cast": "Cast",
    "ActionType_Activate": "Activate",
    "ActionType_Activate_Mana": "Tap for Mana",
    "ActionType_Pass": "Pass",
    "ActionType_FloatMana": "Float Mana",
    "ActionType_Special": "Special",
}

MANA_COLORS = {
    "ManaColor_White": "W",
    "ManaColor_Blue": "U",
    "ManaColor_Black": "B",
    "ManaColor_Red": "R",
    "ManaColor_Green": "G",
    "ManaColor_Colorless": "C",
    "ManaColor_Generic": "X",
}

GRE_MESSAGE_TYPES = {
    "GREMessageType_GameStateMessage",
    "GREMessageType_ConnectResp",
    "GREMessageType_MulliganReq",
    "GREMessageType_ActionsAvailableReq",
    "GREMessageType_DieRollResultsResp",
    "GREMessageType_ChooseStartingPlayerReq",
    "GREMessageType_DeclareAttackersReq",
    "GREMessageType_DeclareBlockersReq",
    "GREMessageType_SelectTargetsReq",
    "GREMessageType_SelectNReq",
    "GREMessageType_GroupReq",
    "GREMessageType_PromptReq",
    "GREMessageType_IntermissionReq",
    "GREMessageType_TimerStateMessage",
    "GREMessageType_UIMessage",
    "GREMessageType_SetSettingsResp",
}

# DB mappings
RARITY_MAP = {0: "Token", 1: "Land", 2: "Common", 3: "Uncommon", 4: "Rare", 5: "Mythic"}
# Colors in DB: position-based ("1"=W, "2"=U, "3"=B, "4"=R, "5"=G)
DB_COLORS = {"1": "W", "2": "U", "3": "B", "4": "R", "5": "G"}
# Types in DB
DB_TYPES = {"1": "Artifact", "2": "Creature", "3": "Enchantment", "4": "Instant", "5": "Land", "8": "Planeswalker", "10": "Sorcery", "14": "Battle"}
