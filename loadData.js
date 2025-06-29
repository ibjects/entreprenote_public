// loadData.js

(async () => {
  console.log("loadData.js starting...");

  let categories, tools;

  try {
    // Fetch data (resolve URLs relative to this script so it works no matter where the page lives)
    [categories, tools] = await Promise.all([
      fetch(new URL("categories.json", import.meta.url)).then((r) => r.json()),
      fetch(new URL("tools.json", import.meta.url)).then((r) => r.json()),
    ]);
    console.log("Fetched from JSON files");
  } catch (err) {
    console.error("Failed to load data from files, using fallback", err);
    // Fallback data
    categories = [
      { id: "ideation", name: "Ideation & Validation" },
      { id: "planning", name: "Business Planning" },
      { id: "branding", name: "Branding & Design" },
      { id: "development", name: "Product Development" },
      { id: "marketing", name: "Marketing & Growth" },
      { id: "sales", name: "Sales & CRM" },
      { id: "funding", name: "Funding & Finance" },
      { id: "operations", name: "Operations & Productivity" },
      { id: "analytics", name: "Analytics & Feedback" },
    ];
    tools = [
      {
        name: "IdeaBuddy",
        description:
          "A comprehensive business planning tool that helps entrepreneurs develop, test, and launch their ideas.",
        url: "https://ideabuddy.com/",
        category: "planning",
      },
      {
        name: "Canva",
        description:
          "A graphic design platform to create social media graphics, presentations, and more.",
        url: "https://www.canva.com/",
        category: "branding",
      },
      {
        name: "Buffer",
        description:
          "A social media management tool to schedule posts and analyze performance.",
        url: "https://buffer.com/",
        category: "marketing",
      },
    ];
  }

  console.log("Loaded categories:", categories.length);
  console.log("Loaded tools:", tools.length);

  /* ---------- DOM references ---------- */
  const catGrid = document.getElementById("categoriesGrid");
  const toolsGrid = document.getElementById("toolsGrid");
  const heading = document.getElementById("toolsHeading");
  const countTxt = document.querySelector(".text-gray-600");

  console.log("Found elements:", {
    catGrid: !!catGrid,
    toolsGrid: !!toolsGrid,
  });

  if (!catGrid || !toolsGrid) return;

  /* ---------- helpers ---------- */
  const catName = (id) =>
    (categories.find((c) => c.id === id) || {}).name || id;

  function renderCategories() {
    catGrid.innerHTML = [
      `<div class="category-item active p-4 border border-gray-200 rounded-lg text-center cursor-pointer" data-category="all">
         <div class="w-12 h-12 mx-auto mb-3 bg-primary/10 rounded-full flex items-center justify-center">
           <i class="ri-more-line text-primary ri-lg"></i>
         </div>
         <h3 class="font-medium">All</h3>
       </div>`,
      ...categories.map(
        (c) => `
       <div class="category-item p-4 border border-gray-200 rounded-lg text-center cursor-pointer" data-category="${c.id}">
         <div class="w-12 h-12 mx-auto mb-3 bg-primary/10 rounded-full flex items-center justify-center">
           <i class="ri-lightbulb-line text-primary ri-lg"></i>
         </div>
         <h3 class="font-medium">${c.name}</h3>
       </div>`
      ),
    ].join("");
  }

  function renderTools() {
    toolsGrid.innerHTML = tools
      .map(
        (t) => `
      <div class="tool-card bg-white border border-gray-200 rounded-lg overflow-hidden shadow-sm" data-category="${
        t.category || t.categoryId
      }">
        <div class="p-5">
          <h3 class="font-semibold text-lg mb-2">${t.name}</h3>
          <p class="text-gray-600 text-sm mb-4">${t.description}</p>
          <div class="flex items-center justify-between">
            <span class="text-xs font-medium px-2 py-1 bg-primary/10 text-primary rounded-full">${catName(
              t.category || t.categoryId
            )}</span>
            <a href="${
              t.url
            }" target="_blank" class="text-primary hover:text-primary-dark font-medium text-sm flex items-center">Visit <i class="ri-external-link-line ml-1"></i></a>
          </div>
        </div>
      </div>`
      )
      .join("");
  }

  function applyFilter(category) {
    let shown = 0;
    toolsGrid.querySelectorAll(".tool-card").forEach((card) => {
      const show = category === "all" || card.dataset.category === category;
      card.style.display = show ? "block" : "none";
      if (show) shown++;
    });

    if (heading)
      heading.textContent =
        category === "all" ? "Tools" : `${catName(category)} Tools`;
    if (countTxt)
      countTxt.textContent = `Showing ${shown} tool${
        shown !== 1 ? "s" : ""
      } for entrepreneurs`;
  }

  function attachCategoryHandlers() {
    catGrid.addEventListener("click", (e) => {
      const item = e.target.closest(".category-item");
      if (!item) return;
      catGrid
        .querySelectorAll(".category-item")
        .forEach((i) => i.classList.remove("active"));
      item.classList.add("active");
      applyFilter(item.dataset.category);
    });
  }

  /* ---------- init ---------- */
  renderCategories();
  renderTools();
  attachCategoryHandlers();
  applyFilter("all");
})();
