import { expect, test } from "@playwright/test"

test("opens topics in tabs and navigates from sidebar search", async ({ page }) => {
  await page.goto("/")

  await expect(page.getByText("Alpha review")).toBeVisible()
  await page.getByRole("button", { name: /Alpha review/i }).click()
  await expect(page.getByText("hello from alpha")).toBeVisible()

  await page.getByPlaceholder("Search").fill("handoff")
  const result = page.getByRole("button", { name: /Beta thread/i }).first()
  await expect(result).toBeVisible()
  await result.click()

  await expect(page).toHaveURL(/\/topics\/.+\?focus=/)
  await expect(page.locator("main").getByText("beta handoff summary")).toBeVisible()
})

test("reveals the thread-map overlay for long desktop topics while keeping the inspector rail", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 700 })
  await page.goto("/")

  await page.getByRole("button", { name: /Alpha review/i }).click()
  await page.locator("[data-ab-topic-thread-scroll-area='true']").evaluate((element) => {
    ;(element as HTMLElement).style.height = "320px"
  })

  await expect(page.getByText("Topic metadata")).toBeVisible()
  await expect(page.locator("[data-ab-thread-map-hotspot='true']")).toBeAttached()
  await expect(page.locator("[data-ab-thread-map='true']")).toHaveAttribute("data-visible", "false")

  await page.locator("[data-ab-thread-map-hotspot='true']").hover()
  await expect(page.locator("[data-ab-thread-map='true']")).toHaveAttribute("data-visible", "true")
  await expect(page.locator("[data-ab-thread-map='true']")).toBeVisible()
  await expect(page.locator("[data-ab-thread-map-marker]").first()).toBeVisible()
})

test("keeps the send button clickable with a tall draft and the inspector scrollable", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 480 })
  await page.goto("/")

  await page.getByRole("button", { name: /Alpha review/i }).click()

  // The inspector renders its own scroll area rather than overflowing its rail.
  await expect(page.locator("[data-ab-inspector-scroll-area='true']")).toBeVisible()

  // Grow the draft so the composer is at its tallest, then confirm the send button is
  // genuinely clickable. The thread-map hotspot overlays the right edge at z-10 and
  // intercepts pointer events over the composer unless the composer outranks it, so a
  // hit-tested click is the assertion that catches the regression.
  await page.getByPlaceholder("Type a message into this topic thread...").fill("line\n".repeat(30))

  const sendButton = page.getByRole("button", { name: "Send" })
  await expect(sendButton).toBeVisible()
  await sendButton.click({ trial: true })

  const box = await sendButton.boundingBox()
  expect(box).not.toBeNull()
  expect(box!.y + box!.height).toBeLessThanOrEqual(480)

  // The thread keeps a non-zero viewport that scrolls, rather than collapsing away.
  const threadScrolls = await page.evaluate(() => {
    const root = document.querySelector("[data-ab-topic-thread-scroll-area='true']") as HTMLElement
    const viewport = root.querySelector("[data-slot='scroll-area-viewport']") as HTMLElement
    return viewport.clientHeight > 0 && viewport.scrollHeight > viewport.clientHeight
  })
  expect(threadScrolls).toBe(true)
})

test("thread map reveals from both the focus effect and pointer handlers", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 700 })
  await page.goto("/")
  await page.getByRole("button", { name: /Alpha review/i }).click()

  // Shrink the thread viewport so the map qualifies (mirrors the existing e2e test).
  await page.locator("[data-ab-topic-thread-scroll-area='true']").evaluate((element) => {
    ;(element as HTMLElement).style.height = "320px"
  })

  const threadMap = page.locator("[data-ab-thread-map='true']")
  await expect(threadMap).toHaveAttribute("data-visible", "false")

  // --- Effect path, isolated -------------------------------------------------
  // Let any prior reveal auto-hide first, so the assertion below can only pass
  // because the find effect revealed the map.
  await expect(threadMap).toHaveAttribute("data-visible", "false", { timeout: 5000 })

  await page.keyboard.press("Control+f")
  await expect(page.getByPlaceholder("Find in this thread")).toBeVisible()
  // Opening find flips findState.open, which the effect observes.
  await expect(threadMap).toHaveAttribute("data-visible", "true", { timeout: 3000 })

  // Close find and wait for auto-hide so the hover path is isolated too.
  await page.keyboard.press("Escape")
  await expect(threadMap).toHaveAttribute("data-visible", "false", { timeout: 5000 })

  // --- Handler path ----------------------------------------------------------
  await page.locator("[data-ab-thread-map-hotspot='true']").hover()
  await expect(threadMap).toHaveAttribute("data-visible", "true")
  await expect(page.locator("[data-ab-thread-map-marker]").first()).toBeVisible()

  // Drag calls dragThreadMapViewport, another handler call site.
  const box = await threadMap.boundingBox()
  expect(box).not.toBeNull()
  if (box) {
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 4)
    await page.mouse.down()
    await page.mouse.move(box.x + box.width / 2, box.y + (box.height * 3) / 4, { steps: 5 })
    await page.mouse.up()
  }
  await expect(threadMap).toBeAttached()
})
