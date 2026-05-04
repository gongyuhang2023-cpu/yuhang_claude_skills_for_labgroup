# 默认排除模式

初始化 `.project-meta/ignore` 时使用以下默认排除规则。

## 版本控制
```
.git/
.svn/
```

## IDE / 编辑器
```
.vscode/
.idea/
.Rproj.user/
*.Rproj
.obsidian/
```

## 运行时 / 缓存
```
node_modules/
__pycache__/
.venv/
venv/
.cache/
*.pyc
```

## R / Quarto 临时文件
```
*_files/
*_cache/
.Rhistory
.RData
```

## 系统文件
```
.DS_Store
Thumbs.db
desktop.ini
```

## 本工具自身
```
.project-meta/
```

## 大型二进制（可选，用户可移除）
```
# *.rds
# *.h5
# *.hdf5
```
