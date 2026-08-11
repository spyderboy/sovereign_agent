#!/bin/bash
# Run this from inside ~/Code/spyderboy after scaffolding with create-next-app

set -e
echo "Setting up spyderboy for Xanadu..."

# sovereign.json
cat > sovereign.json << 'EOF'
{
  "project": "spyderboy",
  "stack": "nextjs",
  "vision": "VISION.md",
  "roadmap": "ROADMAP.md",
  "rules": ".roorules",
  "validator": "npm run build",
  "logs_dir": "logs",
  "registered": "2026-06-23"
}
EOF

# logs dir
mkdir -p logs && touch logs/.gitkeep

# VISION.md
cat > VISION.md << 'EOF'
# spyderboy.com — Vision

## What This Is

A personal studio portal for Jose Antonio Licon (aka Spyderboy). A single-page Next.js site that introduces five active projects and the AI-powered development system behind them. This is not a portfolio for job-seeking. It is not a pitch for investment. It is an introduction to a builder doing interesting work — aimed at peers, colleagues, and the kind of people worth knowing.

## Headline

> One person. Five very different projects. One engine. Shipping daily.

## Subheadline

> I build apps, games, and platforms — powered by an AI development system I built myself.

## Primary Audience

Silicon Valley investment bankers, technologists, and founders who met Tony at a dinner or event and Googled him afterward. They are sophisticated, time-short, and respond to specificity and confidence — not buzzwords or consulting-speak. The goal is to be memorable and worth a follow-up conversation.

## Tone

Confident. Direct. First-person. No corporate language. No "I help clients achieve." No CTAs to "hire me." Reads like a founder's studio page, not a freelancer's resume.

## Sections (in order)

### 1. Nav
- Logo: "spyderboy" wordmark (text only)
- Links: work · engine · contact (anchor links)

### 2. Hero
- Eyebrow: "Jose Antonio Licon · Pittsburgh, PA"
- Headline (4 lines, large): "One person. / Five very different projects. / One engine. / Shipping daily."
- Subhead: "I build apps, games, and platforms — powered by an AI development system I built myself."
- CTAs: "See the work" (scrolls to #projects) | "The engine →" (scrolls to #engine)

### 3. Projects (id="projects")
Five cards in a 2-column grid. Podomus spans full width at bottom.

| Project | Status | One-liner | Tags |
|---|---|---|---|
| Retro Car Radio | Live | Internet radio with a classic car preset interface. Old school vibes, a world of streaming. | iOS · Android · Flutter |
| Galaxain | Coming soon | A space strategy game where stars produce resources, fleets capture territory, and one more turn becomes five. | iOS · Android · Flutter/Flame |
| Magic Task Hat | In development | Personal productivity powered by Agile principles. Your backlog, your sprints, your rules. | Flutter · Firebase · GCP |
| Apartment Manager | In development | Property management for 29 real units. Built to solve a real problem — and it works. | Flutter · Firebase |
| Podomus | Moonshot | Free podcast hosting on GCP free tiers. Start your show. Zero cost, zero excuses. | GCP · Firebase |

Status badge colors:
- Live → green (bg-green-100 text-green-800)
- Coming soon → amber (bg-amber-100 text-amber-800)
- In development → blue (bg-blue-100 text-blue-800)
- Moonshot → purple (bg-purple-100 text-purple-800)

### 4. Engine (id="engine")
Label: "The engine" — Title: "Xanadu"
Two-column layout: description left, stat cards right.

Description:
"An autonomous development loop: local LLMs in a tiered cascade, mechanical error correction, and a planning layer that turns a backlog into running code — unattended. Claude and Gemini sit at the top, handling escalations and architecture. Everything below runs locally. A 2-week plan compresses to 2–3 days of wall-clock time."

Stats:
- Tasks completed autonomously: 535+
- First-pass success rate: ~70%
- LLM tiers in the cascade: 4 + Claude

### 5. Why I Built It (id="why")
Label: "Why I built it"

Opening (large, weighted):
"I started where everyone starts — Claude, Gemini, the usual suspects. I was amazed at what was possible. Then I ran out of tokens."

Body:
"Most people slow down at that point. I bought a maxed-out MacBook Air and started learning local LLMs instead.

What began as a workaround turned into something more interesting: a complete development loop. Product ideation. SWOT analysis. Backlog generation. Code execution across parallel workers. Models that fail over to more capable tiers when a task is too hard. Error patterns that get learned and encoded so they don't burn retries twice.

Now I set it running overnight. By morning, the project is mostly done. When I need more firepower, I spin up RunPod. A week of backlog in an hour, at a cost that doesn't require a VC.

It didn't replace the craft — my background as a full-stack developer and technical product manager is what makes the system work, not what it replaced. I still drive. I still take the wheel when the models hit a wall, or the project needs a pivot. Agile methodology is the backbone of the whole system — not just a buzzword, but the actual structure that keeps five projects moving at once."

Pull quote (left border):
"This isn't a silver bullet. It's a superpower."

### 6. Footer
Left: "Jose Antonio Licon · Pittsburgh · 2026"
Right: LinkedIn · @spyderboy · dev@spyderboy.com

## Tech Stack

- Next.js 14+ App Router
- TypeScript strict
- Tailwind CSS utilities only — no CSS modules
- next/font for Inter
- next/image for all images
- Netlify hosting (build from GitHub)

## Definition of Done

- [ ] Single page loads at spyderboy.com with no errors
- [ ] All five project cards render with correct status badges
- [ ] Engine and Why sections render correctly
- [ ] Smooth scroll anchors work for nav links
- [ ] Mobile-responsive at 375px and 768px
- [ ] OG tags correct for LinkedIn preview
- [ ] Netlify build succeeds from GitHub push
- [ ] npm run build passes with zero TypeScript errors
EOF

# .roorules
cat > .roorules << 'EOF'
# spyderboy.com — Coding Rules for Xanadu Executor

## Stack
- Next.js 14+ App Router (not Pages Router)
- TypeScript strict mode — all files must be .tsx or .ts
- Tailwind CSS — utility classes only, no inline styles, no CSS modules
- next/font for Inter
- next/image for all <img> elements

## Project Structure
```
app/
  layout.tsx        ← root layout, metadata, font setup
  page.tsx          ← main page, assembles all sections
  globals.css       ← Tailwind base/components/utilities + scroll-behavior: smooth
components/
  Nav.tsx
  Hero.tsx
  ProjectCard.tsx
  ProjectGrid.tsx
  EngineSection.tsx
  StatCard.tsx
  WhyBuilt.tsx
  Footer.tsx
lib/
  projects.ts       ← project data array with types
  constants.ts      ← nav links, engine stats, contact info
public/
  (images, favicon)
netlify.toml
```

## Component Rules
- Every component is a default export React functional component
- Props typed with a TypeScript interface in the same file
- No `any` types
- No class components
- Use "use client" only when interactivity is required — prefer server components
- No useState for decorative effects — use Tailwind hover:/focus: utilities

## Tailwind Rules
- Utility classes only — no inline style attributes
- Mobile-first responsive: default = mobile, md: = 768px, lg: = 1024px
- Font weights: font-normal (400) and font-medium (500) only
- Status badge classes:
  - Live: bg-green-100 text-green-800
  - Coming soon: bg-amber-100 text-amber-800
  - In development: bg-blue-100 text-blue-800
  - Moonshot: bg-purple-100 text-purple-800

## Data Rules
- All project data in lib/projects.ts as a typed array
- All constants (nav, stats, contact) in lib/constants.ts
- page.tsx imports data and passes as props — components never import lib/ directly

## TypeScript Rules
- npm run build must pass with zero errors
- All arrays typed (e.g. Project[])
- Use const unless reassignment needed

## Accessibility
- Heading hierarchy: h1 in Hero, h2 for section titles, h3 for card titles
- All images have descriptive alt text
- Nav links are <a> elements with href anchors

## Smooth Scroll
- html { scroll-behavior: smooth } in globals.css
- Nav links: href="#projects", href="#engine", href="#why"
- Sections have matching id attributes

## Netlify
- netlify.toml at project root
- Build command: npm run build
- Publish directory: .next
- Include @netlify/plugin-nextjs

## Anti-patterns — never do these
- Never use CSS modules
- Never hardcode copy in components — text from props or lib/constants.ts
- Never use <img> — always next/image
- Never stub or use TODO comments — every task must be complete and functional
- Never use font-semibold or font-bold
EOF

# ROADMAP.md
cat > ROADMAP.md << 'EOF'
# spyderboy.com — Roadmap

## Week 1 — Day 1: Foundation

- [ ] Add scroll-behavior smooth to html element and verify Tailwind directives are present — touches: `app/globals.css` — done when: globals.css has @tailwind base/components/utilities and html selector with scroll-behavior smooth
- [ ] Update root layout with Inter font, page title "Spyderboy Studio", description, and OG meta tags — touches: `app/layout.tsx` — done when: layout exports metadata with title, description, openGraph title/description/url, and twitter card fields
- [ ] Create Netlify config with Next.js build command and plugin — touches: `netlify.toml` — done when: file has [build] command="npm run build" and [[plugins]] package="@netlify/plugin-nextjs"

## Week 1 — Day 2: Data Layer

- [ ] Create Project type and export typed projects array with all five projects including name, status, oneliner, tags, and icon fields — touches: `lib/projects.ts` — done when: five Project objects export correctly with all fields typed, getStatusClasses(status) utility returns correct Tailwind classes for all four status values
- [ ] Create constants file with NAV_LINKS array, ENGINE_STATS array, and CONTACT object — touches: `lib/constants.ts` — done when: NAV_LINKS has three items (work/engine/contact with href anchors), ENGINE_STATS has three items (label+value), CONTACT has linkedin/twitter/email strings

## Week 1 — Day 3: Components

- [ ] Create Nav component with spyderboy wordmark and three anchor nav links — touches: `components/Nav.tsx` — done when: renders logo text and links to #projects #engine #why, mobile-friendly layout with flex wrap
- [ ] Create StatCard component accepting label and value props — touches: `components/StatCard.tsx` — done when: renders muted small label above large medium-weight value, uses Tailwind bg-gray-50 card style
- [ ] Create Hero component with eyebrow, four-line h1 headline, subhead paragraph, and two anchor CTA buttons — touches: `components/Hero.tsx` — done when: all copy from VISION.md renders correctly, buttons link to #projects and #engine
- [ ] Create ProjectCard component accepting a Project prop — touches: `components/ProjectCard.tsx` — done when: renders icon emoji or placeholder, status badge with correct color classes from getStatusClasses, h3 title, description, and tag pills. Hover darkens border.
- [ ] Create ProjectGrid component rendering all projects from props — touches: `components/ProjectGrid.tsx` — done when: first four cards in md:grid-cols-2 grid, fifth card (Podomus) has col-span-2 and horizontal flex layout on md+
- [ ] Create EngineSection component with two-column layout on md+ — touches: `components/EngineSection.tsx` — done when: left column has h2 Xanadu title and two description paragraphs, right column renders three StatCard components from ENGINE_STATS
- [ ] Create WhyBuilt component with large opening statement, four body paragraphs, and pull quote with left border — touches: `components/WhyBuilt.tsx` — done when: all copy from VISION.md Why section renders, pull quote has border-l-2 border-gray-300 pl-5 treatment
- [ ] Create Footer component with name/year left and contact links right separated by top border — touches: `components/Footer.tsx` — done when: renders border-t, left side "Jose Antonio Licon · Pittsburgh · 2026", right side three links to LinkedIn/Twitter/email from CONTACT constant

## Week 1 — Day 4: Assembly & Polish

- [ ] Assemble main page importing all components and passing correct props — touches: `app/page.tsx` — done when: page renders Nav, section#projects with ProjectGrid, section#engine with EngineSection, section#why with WhyBuilt, Footer — all sections separated by border-t
- [ ] Add responsive single-column mobile layout to ProjectGrid — touches: `components/ProjectGrid.tsx` — done when: cards stack in single column below md breakpoint, Podomus card stacks vertically on mobile
- [ ] Add responsive layout to EngineSection so stat cards stack below text on mobile — touches: `components/EngineSection.tsx` — done when: single column below md, two columns on md+
- [ ] Audit full page for TypeScript errors and fix all — touches: any file with type errors — done when: npm run build completes with zero type errors
- [ ] Verify all anchor links resolve correctly and smooth scroll works — touches: `components/Nav.tsx`, `app/page.tsx` — done when: clicking work/engine/contact scrolls to correct sections
- [ ] Add canonical URL, robots meta, and verify OG tags are complete — touches: `app/layout.tsx` — done when: canonical https://spyderboy.com, robots index follow, og:image path set
EOF

echo ""
echo "✓ All Xanadu files created:"
echo "  VISION.md"
echo "  .roorules"
echo "  ROADMAP.md"
echo "  sovereign.json"
echo "  logs/"
echo ""
echo "Next: cd ~/Code/Xanadu/sovereign_agent && ./standup --project ~/Code/spyderboy"
