# Target and network. One project-owned VPC; never adopt a shared/default VPC.
data "aws_availability_zone" "selected" {
  for_each = toset(var.availability_zones)
  name     = each.value
  state    = "available"
  lifecycle {
    postcondition {
      condition = self.zone_type == "availability-zone" && self.region == var.aws_region && !contains([
        "use1-az3", "usw1-az2", "apne1-az3", "cac1-az3"
      ], self.zone_id)
      error_message = "Selected AZ is not a supported standard CloudFront VPC-origin AZ in this Region."
    }
  }
}

locals {
  subnet_bits = 24 - tonumber(split("/", var.vpc_cidr)[1])
  private_subnets = {
    for index, az in var.availability_zones : az => cidrsubnet(var.vpc_cidr, local.subnet_bits, index)
  }
  cloudfront_url = "https://${aws_cloudfront_distribution.app.domain_name}"
  cognito_domain = "https://${var.project_name}-${var.aws_account_id}.auth.${var.aws_region}.amazoncognito.com"
}

resource "aws_vpc" "app" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.project_name }
}

resource "aws_internet_gateway" "app" {
  vpc_id = aws_vpc.app.id
}

resource "aws_subnet" "private" {
  for_each                = local.private_subnets
  vpc_id                  = aws_vpc.app.id
  availability_zone       = data.aws_availability_zone.selected[each.key].name
  cidr_block              = each.value
  map_public_ip_on_launch = false
  tags                    = { Name = "${var.project_name}-private-${each.key}" }
}

resource "aws_subnet" "nat" {
  vpc_id                  = aws_vpc.app.id
  availability_zone       = data.aws_availability_zone.selected[var.availability_zones[0]].name
  cidr_block              = cidrsubnet(var.vpc_cidr, local.subnet_bits, 2)
  map_public_ip_on_launch = false
  tags                    = { Name = "${var.project_name}-nat" }
}

resource "aws_eip" "nat" {
  domain = "vpc"
}

resource "aws_nat_gateway" "app" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.nat.id
  depends_on    = [aws_internet_gateway.app]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.app.id
}
resource "aws_route" "internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.app.id
}
resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.nat.id
  route_table_id = aws_route_table.public.id
}
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.app.id
}
resource "aws_route" "nat" {
  route_table_id         = aws_route_table.private.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.app.id
}
resource "aws_route_table_association" "private" {
  for_each       = aws_subnet.private
  subnet_id      = each.value.id
  route_table_id = aws_route_table.private.id
}

# Separate security groups/rules avoid ALB -> VPC origin -> managed SG cycles.
resource "aws_security_group" "alb" {
  name        = "${var.project_name}-alb"
  description = "Only CloudFront VPC-origin SG ingress, only task SG egress"
  vpc_id      = aws_vpc.app.id
}
resource "aws_security_group" "task" {
  name        = "${var.project_name}-task"
  description = "Only ALB ingress; HTTPS egress via NAT"
  vpc_id      = aws_vpc.app.id
}
resource "aws_vpc_security_group_ingress_rule" "task" {
  security_group_id            = aws_security_group.task.id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}
resource "aws_vpc_security_group_egress_rule" "alb" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.task.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}
resource "aws_vpc_security_group_egress_rule" "task_https" {
  security_group_id = aws_security_group.task.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}
# AmazonProvidedDNS is not filtered by SGs; no broad DNS egress is needed.

