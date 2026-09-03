# Self-Generating World: Architecture Analysis

## 1. CURRENT STATE AUDIT: HARDCODED vs AI-GENERATED

### 1.1 — `bestiary.py` — 100% HARDCODED
- **14 creatures** with fixed names and descriptions in `BEASTIES` dict
- Appear on deterministic schedules (season stage, run position)
- Same text every time, no AI involvement
- **Blocker**: AI cannot create new creatures. The bestiary is a closed set.

### 1.2 — `lore.py` — MIXED (Offline Fallback + AI)
- **14 locations** hardcoded in `_PLACES` with fixed Russian descriptions
- **3 unlocked locations** in `_UNLOCKED_PLACES`
- **~80 card templates**: 30 risk, 30 care, 25 cunning — all hardcoded titles, descriptions, consequences
- **Image prompts**: 3 fixed templates (risk/care/cunning)
- `compose_chapter()` assembles from these templates deterministically when AI is off
- **When AI IS on**: `_free_story_llm()` generates the narrative, but the card titles/descriptions/consequences come from the hardcoded pools in `lore.py`
- **Blocker**: AI writes prose around fixed cards. Cards themselves are never AI-generated.

### 1.3 — `season.py` — 100% HARDCODED (Deterministic)
- Season arc structure: 4 acts with fixed tonal descriptions
- Villain events: 4 stages × 4-5 events each, all hardcoded
- Heretic events: same pattern
- Alignment system: 2-axis drift with fixed step tables
- **Blocker**: AI cannot create new villains, new events, or new season arcs.

### 1.4 — `story.py` — MIXED (AI + Hardcoded)
- `BASE_PROMPT`: hardcoded DM instructions
- `CHARACTER_MICRO_PROMPTS`: 10 fixed character descriptions
- `_VOICE_CARDS`: 7 fixed voice profiles with examples
- **AI generates**: chapter text, epilogue text
- **AI does NOT generate**: character names, locations, card options, consequences
- **Blocker**: Characters are fixed. The AI writes ABOUT them but doesn't create them.

### 1.5 — `rounds.py` — MIXED
- Day composition pipeline: assembles season block, villain block, heretic block, echoes, etc.
- **AI generates**: chapter text, cover image prompt
- **Hardcoded**: card selection from pools, win rules, sealed days, arc missions
- **Blocker**: The "choice architecture" is fixed — 3 cards from predefined pools.

### 1.6 — `prologue.py` — 100% HARDCODED
- 7-day season 1 prologue with fixed beats
- 3-day season 2+ prologue
- **Blocker**: First week is always the same story.

### 1.7 — `callings.py` — 100% HARDCODED
- 7 fixed callings with fixed descriptions and unlock conditions
- **Blocker**: No AI-generated character classes.

### 1.8 — `relations.py` — 100% HARDCODED
- 4 NPCs with fixed names (liner, archivist, master, heretic)
- Fixed shift table per tag
- Fixed tone descriptions
- Fixed NPC wants
- **Blocker**: NPCs are fixed. Their reactions are deterministic.

### 1.9 — `art_director.py` — MIXED (AI + Hardcoded)
- **AI generates**: visual bible (palette, lighting, motifs, scene)
- **Hardcoded**: 4 palette rotations, composition pools, style suffix, character motifs
- Character visual descriptors are fixed
- **Blocker**: Art style is constrained to existing palettes and templates.

### 1.10 — `story_arc.py` — 100% HARDCODED
- 4 fixed acts with fixed missions, whispers, teasers
- Fixed howl signs, arc secrets
- Fixed card titles per act
- **Blocker**: Monthly arc is identical every season.

### 1.11 — `config.py` — HARDCODED WORLD BRIEF
- `world_name` and `world_brief`: fixed world description
- **Blocker**: The world itself is defined in config, not generated.

### 1.12 — `npc_cog.py` — HARDCODED (Deterministic Templates)
- 4 NPCs × 4 moods = 16 inner thought templates
- Fixed motivations and actions per NPC+mood
- **Blocker**: NPC behavior is template-matched, not generated.

### 1.13 — `consequence_trees.py` — HARDCODED
- 4 consequence trees with fixed stages and choices
- **Blocker**: Consequences are predetermined trees, not AI-generated cascades.

### 1.14 — `scar_rules.py` — HARDCODED
- 9 scar rules with fixed trigger tags, streaks, durations
- **Blocker**: World scars are predefined.

### 1.15 — `emotional_state.py` — HARDCODED
- 3 emotion axes with fixed shifts
- **Blocker**: Emotional responses are fixed.

### 1.16 — `dynamic_rules.py` — HARDCODED
- 8 rule overrides with fixed conditions
- **Blocker**: Dynamic rules are predefined.

### 1.17 — `broadcast.py` — HARDCODED UI
- Fixed message formats, keyboard layouts
- **Blocker**: Player-facing presentation is rigid.

---

## 2. FEASIBILITY ASSESSMENT

### What the user wants:
1. AI generates characters (name, personality, flaws, virtues, moral compass)
2. AI generates locations (specific rooms, corridors, atmospheres)
3. AI generates cascading consequences
4. AI generates art prompts reflecting actual story
5. AI generates contextually meaningful choices
6. Players' choices reshape the world permanently
7. No artificial boundaries

### What the architecture currently supports:
- ✅ AI generates narrative prose (chapter text)
- ✅ AI generates visual bible (art direction)
- ✅ Some state persistence (scars, echoes, emotions, pack state, relations)
- ✅ Consequence tracking (echoes, scars, consequence trees)
- ✅ Memory system (recall_beats, semantic search)
- ✅ DayProjection as unified fact object

### What needs to change:
- ❌ Character creation → must be AI-generated, persisted, referenced
- ❌ Location creation → must be AI-generated, persisted, referenced
- ❌ Card/choice generation → must be AI-generated from context
- ❌ Consequence cascading → must be AI-designed, not template-matched
- ❌ World evolution → must persist AI decisions across days
- ❌ NPC creation → must be AI-generated, not fixed 4

---

## 3. CONCRETE BLOCKERS

### Blocker 1: The "Closed Card Pool"
The core game loop is: AI writes 3 cards from `_RISK_PATHS` / `_CARE_PATHS` / `_CUNNING_PATHS`. These are ~80 hardcoded templates. Even when AI generates the narrative, the CHOICES are predetermined.

**Fix**: AI generates 3 choices from the current world state. Each choice has: title, description, consequence hint, tag. Stored in DB, not selected from pool.

### Blocker 2: Fixed Characters
5 pack dogs (Баркод, Стежка, Вектор, Пиксель, Безымянная) + 4 NPCs (Лайнер, Старый дневник, Еретик, Администратор) are hardcoded with fixed personalities, voices, visual descriptors.

**Fix**: AI generates characters at season start. Each character has: name, personality, flaws, virtues, moral compass, voice pattern, visual descriptor. Stored in DB. Prompt references DB characters.

### Blocker 3: Fixed Locations
14 locations in `_PLACES` + 3 unlocked. The world has 17 places total, forever.

**Fix**: AI generates locations as the pack explores. Each location has: name, description, atmosphere, connections, state (damaged/intact/transformed). Stored in DB.

### Blocker 4: Template-Matched Consequences
`consequence_trees.py` has 4 hardcoded trees. `scar_rules.py` has 9 rules. Consequences are if-then, not emergent.

**Fix**: AI designs consequences. When a choice is made, the AI generates what happens next, stores it as a "world event" with cascading effects. Multiple events can chain.

### Blocker 5: No World State Persistence
The system tracks: alignment axes, NPC relations (4 NPCs × int), emotions (3 ints), pack needs (4 ints), scars (list of keys), echoes (list of text). This is ~20 numbers and some text.

A self-generating world needs: current locations, their states, characters (with personalities and memories), active events, world history, causal chains.

### Blocker 6: Fixed Season Arc
`story_arc.py` has 4 acts with fixed missions, whispers, secrets. Every season tells the same story.

**Fix**: AI generates the season arc at creation: acts, turning points, missions, secrets. Stored in DB.

### Blocker 7: No Character Memory
NPCs have a sentiment score (-3..+3) and fixed template responses. They don't remember specific events.

**Fix**: Each NPC gets a memory log: what happened to them, what they witnessed, what they owe. AI references this in prompts.

---

## 4. ARCHITECTURE PROPOSAL: SELF-GENERATING WORLD

### 4.1 — World State Table (New)
```
world_state
  id, season_key, day_index
  locations_json     — {name, description, atmosphere, connections, state, created_day}
  characters_json    — {name, personality, flaws, virtues, moral_compass, voice, visual, type: pack/npc/creature}
  active_events_json — [{type, description, affected_locations, affected_characters, expires_day}]
  world_rules_json   — [{rule_id, description, trigger, effect, active}]
  world_facts_json   — [strings: "the old bridge burned on day 5", "NPC X owes the pack"]
```

### 4.2 — AI-Generated Day Pipeline
```
1. LOAD world_state from DB
2. BUILD prompt:
   - world_state (locations, characters, events, rules, facts)
   - recent_beats (last 7 days)
   - echoes (surfaced consequences)
   - scars, emotions, pack_needs
   - season context (act, day N of M)
3. AI GENERATES:
   - chapter_text (narrative)
   - 3 choices (title, description, consequence_hint, tag)
   - world_changes (new locations, damaged locations, new characters, character changes, new events)
   - art_prompt (visual bible)
4. STORE world_changes in DB
5. PLAYER VOTES on choice
6. WINNING choice triggers:
   - echo generation
   - scar check
   - emotion shift
   - pack_needs shift
   - NPC relation shift
   - world_state mutation (from AI's consequence_hint)
```

### 4.3 — Character Generation
At season start (or when new characters are introduced):
```
AI PROMPT:
"You are creating a character for a dystopian dog world.
 Type: [pack_member / npc / creature]
 Context: the pack is in [location], dealing with [recent_events].
 Generate: name, personality (2-3 traits), flaw, virtue, moral_compass (axis), 
           voice_pattern (how they speak), visual_descriptor (for art), 
           relationship_to_pack (starting sentiment)."
```
Stored in `world_state.characters_json`. Referenced in all prompts.

### 4.4 — Location Generation
When the pack explores new territory:
```
AI PROMPT:
"The pack is moving from [current_location] into unknown territory.
 Previous choices: [recent_beats].
 World state: [active_scars, emotions, events].
 Generate a new location: name, description (2-3 sentences), atmosphere, 
   what makes it unique, what danger it holds, what it connects to."
```
Stored in `world_state.locations_json`.

### 4.5 — Choice Generation
Each day, instead of selecting from hardcoded pools:
```
AI PROMPT:
"Current location: [location_description].
 Active events: [events].
 Pack state: [emotions, needs, relations].
 Recent history: [last 3 beats].
 
 Generate 3 choices:
 1. [title] — [description]. Consequence: [what happens if chosen]. Tag: [risk/care/cunning]
 2. [title] — [description]. Consequence: [what happens if chosen]. Tag: [risk/care/cunning]
 3. [title] — [description]. Consequence: [what happens if chosen]. Tag: [risk/care/cunning]
 
 Each choice must be contextually meaningful to the current situation.
 No two choices should be interchangeable."
```

### 4.6 — Consequence Cascading
After a choice wins:
```
AI PROMPT:
"The pack chose: [winning_choice].
 Location: [current_location].
 World state: [events, scars, relations].
 
 Generate consequences:
 1. Immediate effect (what happens right now)
 2. Delayed effect (what happens in 2-5 days)
 3. World change (how does this alter the location/world)
 4. Character impact (how does this affect NPCs/pack members)"
```
Stored as events in `world_state.active_events_json`.

### 4.7 — Minimum Viable Path

**Phase 1: AI-Generated Choices (1-2 weeks)**
- Remove hardcoded card pools from `lore.py`
- Add choice generation to the LLM prompt
- Store generated choices in DB (extend Card model or new table)
- Everything else stays the same

**Phase 2: AI-Generated Locations (2-3 weeks)**
- Add `locations` table or JSON field to world_state
- AI generates location when pack moves to new area
- Location persists and evolves
- Replace `_PLACES` pool with DB-backed locations

**Phase 3: AI-Generated Characters (2-3 weeks)**
- Add `characters` table or JSON field
- AI generates pack members and NPCs at season start
- Characters have persistent personalities, memories
- Replace fixed voice cards and micro-prompts with DB-backed characters

**Phase 4: Consequence Cascading (3-4 weeks)**
- Replace `consequence_trees.py` with AI-generated consequences
- Each choice generates immediate + delayed effects
- Effects chain: choice → event → location change → character reaction → new event
- World state accumulates a "history log"

**Phase 5: Full World Generation (4-6 weeks)**
- AI generates season arcs
- AI generates world rules (not just overriding hardcoded ones)
- AI generates the bestiary
- Everything from the original vision is possible

---

## 5. WHAT ALREADY EXISTS THAT CAN BE REPURPOSED

### 5.1 — DayProjection (`projection.py`)
**Already**: Unified fact object for a completed day. Immutable, consumed by all systems.
**Repurpose**: Extend to include world_state changes. After tally, projection captures what the AI decided about the world.

### 5.2 — Scar Rules (`scar_rules.py`)
**Already**: Mechanism for world changes from player choices. Rules check streaks → create scars → affect locations.
**Repurpose**: Make AI generate the scar rules instead of hardcoding them. The framework (streak → scar → effect) is solid; just need AI to fill in the specifics.

### 5.3 — Consequence Trees (`consequence_trees.py`)
**Already**: Multi-stage branching consequences. DB-tracked active branches.
**Repurpose**: Replace hardcoded trees with AI-generated trees. The data model (branch_key, current_stage, history_json) is ready for dynamic content.

### 5.4 — Echoes (`echoes.py`)
**Already**: Delayed consequences that surface days later. Strength-based fading and chaining.
**Repurpose**: Perfect for AI-generated delayed effects. Just need to let AI write the echo descriptions instead of using card consequences.

### 5.5 — NPC Relations (`relations.py`)
**Already**: Sentiment tracking per NPC, shift per tag.
**Repurpose**: Add memory logs. Keep the sentiment system but let AI generate the specific reactions based on history.

### 5.6 — Pack State (`pack_state.py`)
**Already**: Survival mechanics (hunger, thirst, health, death).
**Repurpose**: Expand to include "pack resources" beyond survival: food stores, tools, shelter quality, territory control.

### 5.7 — Memory System (`memory.py`)
**Already**: Semantic search over past beats. Embedding-based recall.
**Repurpose**: Essential for consistency. When AI generates a location on day 3, memory system ensures it's referenced correctly on day 30.

### 5.8 — NPC CoG (`npc_cog.py`)
**Already**: Chain-of-thought for NPC behavior.
**Repurpose**: Replace template-matched thoughts with AI-generated thoughts. The framework is perfect; just need to make the actual text AI-generated.

### 5.9 — Emotional State (`emotional_state.py`)
**Already**: 3-axis emotional tracking with phase detection.
**Repurpose**: Keep as-is. This is player-driven and works well.

### 5.10 — Dynamic Rules (`dynamic_rules.py`)
**Already**: Rule overrides based on world state.
**Repurpose**: Let AI generate new rules as the world evolves. The override framework is ready.

### 5.11 — Art Director (`art_director.py`)
**Already**: AI generates visual bible, character motifs.
**Repurpose**: Extend to include AI-generated character visual descriptors from the characters table.

### 5.12 — Plugin System (`plugins.py`)
**Already**: Extensible prompt injection system.
**Repurpose**: World generation modules can be plugins that inject world_state into prompts.

---

## 6. CRITICAL ARCHITECTURE INSIGHT

The current system is a **deterministic story generator with AI prose**. The skeleton is hardcoded; AI fills in the flesh.

The vision is an **AI-generated world with deterministic state tracking**. AI creates the skeleton AND the flesh; the system only tracks what happened.

The key insight: **the system needs to shift from "AI writes around fixed choices" to "AI generates choices AND their consequences, system tracks the results."**

The existing infrastructure (DB models, projection, scars, echoes, memory, NPC relations, pack state, emotional state) provides the TRACKING layer. What's missing is the GENERATION layer — letting AI create the content that gets tracked.

The minimum viable path is: **start with choices, then locations, then characters, then consequences, then the full world.** Each phase builds on the last and delivers user-visible improvement immediately.
