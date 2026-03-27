# Docker Compose 部署支持 Spec

## Why
当前项目主要通过本机 Python 直接运行，环境准备和依赖安装步骤较多。增加 Docker Compose 能降低部署门槛并提升环境一致性。

## What Changes
- 新增 `docker-compose.yml`，提供一键启动 QQ Bot 的容器编排能力
- 在 Compose 中定义服务构建方式、端口映射、重启策略与基础环境变量
- 挂载运行期需要持久化或可配置的关键目录与配置文件
- 提供最小可用默认配置，支持通过 `.env` 覆盖

## Impact
- Affected specs: 部署方式、运行环境管理
- Affected code: `docker-compose.yml`（新增）、容器构建相关文件（按需复用现有 Dockerfile）

## ADDED Requirements
### Requirement: Compose 一键启动能力
系统 SHALL 提供可直接执行的 Docker Compose 配置，使用户在安装 Docker 后可启动项目核心服务。

#### Scenario: 成功启动容器
- **WHEN** 用户在项目根目录执行 `docker compose up -d`
- **THEN** 服务容器成功启动且进入运行状态

### Requirement: 运行参数可配置
系统 SHALL 支持通过环境变量配置关键运行参数，并提供默认值以保证开箱可用。

#### Scenario: 通过环境变量覆盖端口
- **WHEN** 用户在 `.env` 中设置端口变量
- **THEN** Compose 使用用户设置的端口映射启动服务

### Requirement: 运行数据持久化
系统 SHALL 将关键配置与运行数据通过挂载方式持久化，避免容器重建导致数据丢失。

#### Scenario: 容器重建后配置保留
- **WHEN** 用户删除并重新创建服务容器
- **THEN** 已挂载的配置与运行数据仍可继续使用

## MODIFIED Requirements
### Requirement: 本地运行方式扩展
项目原有 Python 本机运行方式保留，同时新增 Compose 作为并行部署选项。

## REMOVED Requirements
### Requirement: 无
**Reason**: 本次变更仅新增部署能力，不移除现有能力  
**Migration**: 无需迁移
