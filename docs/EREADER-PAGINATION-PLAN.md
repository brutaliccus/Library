# E-Reader Pagination: Discrete Page Experience

## Goal

Each "page" should be a **discrete unit** with fixed top and bottom bounds (based on screen size)—like a physical book page or the cover image. No scrolling within a page. Font size determines how much content fits per page.

## Current Problems

1. **Non-fullscreen**: Each chapter renders as one giant page (no viewport splitting).
2. **Fullscreen**: Requires scrolling to reach the bottom before paging.
3. **Inconsistent**: Behavior differs between fullscreen and non-fullscreen.

## Root Cause

1. **Layout**: The content container may not have a definite height in non-fullscreen (flex layout can fail to constrain).
2. **Scroll-based approach**: Using `overflow-y: auto` + `scrollTop` allows scrolling within the content instead of discrete page flips.
3. **Kavita content size**: Kavita's `book-page` returns pre-split HTML. If each chunk is small (e.g. one xhtml file), `scrollHeight` may be less than viewport → 1 page. If chunks are large, we need to split them.

---

## Implementation Plan

### Phase 1: Fixed-Height Viewport (No Scrolling)

**Objective**: Ensure the reading area has a fixed height and shows exactly one "page" at a time—no scrollbar, no overflow.

#### 1.1 Layout Structure

```
┌─────────────────────────────────────┐
│ Header (shrink-0)                   │
├─────────────────────────────────────┤
│ ┌─────────────────────────────────┐ │
│ │ Page viewport (flex-1 min-h-0)   │ │  ← FIXED HEIGHT = remaining space
│ │ overflow: hidden                │ │
│ │                                 │ │
│ │  ┌───────────────────────────┐  │ │
│ │  │ Content (translateY)      │  │ │  ← Clipped by parent
│ │  │ Full height, no scroll     │  │ │
│ │  └───────────────────────────┘  │ │
│ └─────────────────────────────────┘ │
├─────────────────────────────────────┤
│ Footer (shrink-0)                   │
└─────────────────────────────────────┘
```

- **Container**: `overflow: hidden` (not `overflow-y: auto`). Height = `flex-1` with `min-h-0` so it gets a definite height from the flex parent.
- **Content**: Rendered at full height. We use `transform: translateY(-N * pageHeight)` to show the Nth "page". The container clips the overflow.
- **No scrollbar**: User cannot scroll. Next/Prev are the only way to change view.

#### 1.2 Key CSS Changes

- Replace `overflow-y-auto` with `overflow-hidden` on the page container.
- Ensure the main content area uses `flex-1 min-h-0` so it receives a computed height.
- Use `height: 100%` or explicit height on the viewport div so `pageHeight` = `container.clientHeight` is reliable.

#### 1.3 Both Modes Use Same Layout

- **Fullscreen**: `fixed inset-0` gives definite viewport. Child flex layout works.
- **Non-fullscreen**: `min-h-screen flex flex-col` — the outer div is at least viewport height. The middle section (`flex-1 min-h-0`) must receive the remaining space. This can fail if an ancestor doesn't constrain height. Ensure the entire chain uses `min-h-0` where needed.

---

### Phase 2: Transform-Based Paging (No Scroll)

**Objective**: Show one page at a time via `translateY`. No scroll events, no scrollTop.

#### 2.1 Algorithm

1. **pageHeight** = `container.clientHeight` (the visible area).
2. **totalViewportPages** = `ceil(contentRef.scrollHeight / pageHeight)`.
3. **Current view**: `transform: translateY(-viewportPage * pageHeight)` on the content wrapper.
4. **Next/Prev**: Update `viewportPage` only. No scroll.

#### 2.2 Content Wrapper

- The content div must NOT be inside a scrollable parent. It should be inside `overflow: hidden`.
- The content div has the full rendered HTML. Its natural height is `scrollHeight`.
- We translate it up so the "window" (container) shows the right slice.

---

### Phase 3: Reliable Height in Non-Fullscreen

**Objective**: Fix the layout so `container.clientHeight` is correct in both modes.

#### 3.1 Diagnostic

- Log `container.clientHeight`, `content.scrollHeight`, `pageHeight`, `totalViewportPages` when content loads.
- If `clientHeight` is 0 or very small in non-fullscreen, the flex layout is wrong.

#### 3.2 Fix Strategy

- Ensure the reading area's parent chain: `div (min-h-screen) → div (flex-1 overflow-hidden min-h-0) → main (flex-1 min-h-0) → viewport div (flex-1 min-h-0 overflow-hidden)`.
- The viewport div should have `height: 100%` or `flex: 1` and `min-height: 0` so it gets a computed height.
- Consider using `height: 100vh` minus header/footer for the viewport when not fullscreen, if flex is unreliable.

#### 3.3 Fallback: Explicit Height

If flex is unreliable, use `calc(100vh - headerHeight - footerHeight)` for the viewport height. Measure header/footer once or use CSS variables.

---

### Phase 4: Kavita Content Size

**Objective**: Handle the case when Kavita returns small HTML chunks (scrollHeight < viewport).

#### 4.1 If scrollHeight < pageHeight

- We get `totalViewportPages = 1` (correct for that chunk).
- The issue: we might have many Kavita "pages" per chapter, each returning a small chunk. We'd need to fetch and concatenate them to get meaningful viewport pagination.

#### 4.2 Option A: Concatenate Kavita Pages

- When loading a chapter, fetch `page=0`, `page=1`, ... up to `bookInfo.pages - 1` (or until we have the full chapter).
- Concatenate HTML (strip duplicate `<html>`, `<body>`, etc.).
- Render the full chapter. Then `scrollHeight` reflects the whole chapter.
- Paginate client-side: `totalViewportPages = ceil(scrollHeight / pageHeight)`.

#### 4.3 Option B: Accept Kavita's Chunks

- Each Kavita page = one "page" in our UI if it's small.
- If a Kavita chunk is larger than viewport, we split it (current approach).
- Simpler but less control.

#### 4.4 Recommendation

- **Phase 4a**: First fix layout (Phases 1–3). Verify that when we have sufficient content (scrollHeight > viewport), we get multiple pages.
- **Phase 4b**: If Kavita consistently returns small chunks, implement concatenation (Option A).

---

### Phase 5: Font Size and Reflow

**Objective**: When font size changes, recalculate pages.

- The measure effect already depends on `settings.fontSize` and `settings.fontFamily`.
- After content reflows, `scrollHeight` changes. We recompute `totalViewportPages`.
- Ensure we don't leave `viewportPage` out of bounds (e.g. clamp to `totalViewportPages - 1`).

---

### Phase 6: Edge Cases

1. **Very short chapter**: 1 viewport page. Next goes to next Kavita page/chapter.
2. **Images**: Ensure images have `max-width: 100%` so they don't overflow. They'll affect `scrollHeight` and thus page count.
3. **Resize/rotate**: ResizeObserver already triggers measure. Should work.
4. **TOC navigation**: When jumping to a chapter, reset `viewportPage` to 0 and load new content.

---

## Implementation Checklist

- [ ] **1.1** Change container from `overflow-y-auto` to `overflow-hidden`
- [ ] **1.2** Use `transform: translateY` for paging (remove scroll-based logic)
- [ ] **1.3** Remove scroll event listener
- [ ] **2.1** Ensure viewport div has definite height (flex or calc)
- [ ] **2.2** Add diagnostic logging (or remove after verification)
- [ ] **3.1** Test in both fullscreen and non-fullscreen; verify `clientHeight` > 0
- [ ] **3.2** If needed, use `calc(100vh - Xpx)` for viewport height
- [ ] **4.1** If scrollHeight is consistently small, implement chapter concatenation
- [ ] **5.1** Verify font size change triggers remeasure and page count update

---

## File Changes

| File | Changes |
|------|---------|
| `frontend/src/pages/Ereader.tsx` | Layout (overflow-hidden), transform-based paging, remove scroll logic, ensure flex height chain |

---

## Success Criteria

1. **No scrolling**: User cannot scroll within a page. Only Next/Prev change the view.
2. **Fixed bounds**: Each page has a fixed top and bottom (the viewport).
3. **Font size**: Changing font size changes the number of pages and what fits per page.
4. **Consistent**: Same behavior in fullscreen and non-fullscreen.
5. **Discrete pages**: Like the cover image—one self-contained unit per view.
