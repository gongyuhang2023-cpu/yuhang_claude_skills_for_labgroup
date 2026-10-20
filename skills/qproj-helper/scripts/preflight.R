# qproj 升级前的静态预检（只读，不写任何文件）
#
# 0.2.0 起两道检查从警告变成渲染中止：path_source() 越序读取（qproj_order_error）、
# params$name 与文件名不符（qproj_identity_error）。本脚本在渲染前把它们找出来，
# 顺带列出手拼路径读别的步骤 d<step>/ 的地方（bypass）与 _quarto.yml 的 cache 设置。
#
# 用法：Rscript preflight.R <项目根> [<项目根> ...]
#   spark（不落盘）：ssh spark 'cd <目录> && /opt/bio/bin/Rscript -e "source(file(\"stdin\"))" --args <项目根>' < preflight.R
#
# 顺序判定直接调 qproj 内部的 step_is_before()，与包里的检查同一份逻辑；
# qproj 改名或删掉它时脚本会在 get() 处报错，而不是静默放行。
# 只认字面参数：path_source(变量, ...) 这类调用查不到，qg 会把它们记成 external 节点。
roots <- commandArgs(trailingOnly = TRUE)
stopifnot(length(roots) > 0, utils::packageVersion("qproj") >= "0.2.0")
is_before <- get("step_is_before", envir = asNamespace("qproj"))
`%||%` <- function(a, b) if (is.null(a)) b else a

scan_workflow <- function(wf) {
  qmds <- list.files(wf, pattern = "\\.qmd$", full.names = TRUE)
  qmds <- qmds[!grepl("^(_|readme\\.qmd$)", basename(qmds), ignore.case = TRUE)]
  cfg <- tryCatch(qproj::proj_workflow_config(wf), error = function(e) NULL)
  rnd <- if (is.null(cfg)) list() else cfg$render %||% list()
  strip <- function(x) sub("\\.qmd$", "", x %||% character(0))
  first <- strip(rnd$first); last <- strip(rnd$last)
  out <- character(0)
  for (f in qmds) {
    txt <- readLines(f, warn = FALSE, encoding = "UTF-8")
    if (!any(grepl("proj_create_dir_target", txt, fixed = TRUE))) next
    stem <- sub("\\.qmd$", "", basename(f))
    all <- paste(txt, collapse = "\n")
    # perl = TRUE 不能省：默认 TRE 引擎把方括号里的 \n 当成字面的 n，名字会在第一个 n 前被截断
    yml <- regmatches(all, regexpr("params:\\s*\\n\\s+name:\\s*\"?[^\"\\n]+", all, perl = TRUE))
    pname <- if (length(yml)) sub(".*name:\\s*\"?", "", yml) else NA_character_
    if (!is.na(pname) && !identical(pname, stem))
      out <- c(out, sprintf("  IDENTITY  %s: params$name = \"%s\"", basename(f), pname))
    cur <- if (is.na(pname)) stem else pname
    for (i in grep("path_source\\(\\s*\"", txt)) {
      srcs <- regmatches(txt[i], gregexpr("path_source\\(\\s*\"[^\"]+\"", txt[i]))[[1]]
      for (s in sub("path_source\\(\\s*\"", "", sub("\"$", "", srcs))) {
        if (!is_before(s, cur, first, last))
          out <- c(out, sprintf("  ORDER     %s:%d reads \"%s\"", basename(f), i, s))
      }
    }
    for (i in grep("here::here\\(\\s*path_raw\\s*,\\s*\"d[0-9]{2,3}-", txt)) {
      tgt <- sub(".*here::here\\(\\s*path_raw\\s*,\\s*\"d([0-9]{2,3}-[^\"]+)\".*", "\\1", txt[i])
      if (!grepl("^00-", tgt) && !identical(tgt, cur))
        out <- c(out, sprintf("  BYPASS    %s:%d reads d%s", basename(f), i, tgt))
    }
  }
  q <- readLines(file.path(wf, "_quarto.yml"), warn = FALSE)
  cache <- trimws(grep("^\\s*cache:", q, value = TRUE))
  c(sprintf("%s  [_quarto.yml %s]", wf, if (length(cache)) paste(cache, collapse = "; ") else "cache unset"),
    if (length(out)) out else "  (no identity/order/bypass hits)")
}

for (root in roots) {
  wfs <- unique(dirname(list.files(root, pattern = "^_quarto\\.yml$", recursive = TRUE, full.names = TRUE)))
  wfs <- wfs[!grepl("/(data|renv|_freeze|\\.quarto)(/|$)|_files$|/manuscript$", wfs)]
  for (wf in wfs) cat(scan_workflow(wf), sep = "\n")
}
